#include <atomic>
#include <chrono>
#include <limits>
#include <mutex>
#include <thread>
#include <behaviortree_cpp/bt_factory.h>
#include <ament_index_cpp/get_package_share_directory.hpp>
#include <rclcpp/rclcpp.hpp>
#include <rclcpp_action/rclcpp_action.hpp>
#include <produce_order_interfaces/action/run_fulfillment.hpp>
#include <produce_order_interfaces/srv/fulfillment_step.hpp>

using Step = produce_order_interfaces::srv::FulfillmentStep;
using Run = produce_order_interfaces::action::RunFulfillment;
using Handle = rclcpp_action::ServerGoalHandle<Run>;
using namespace std::chrono_literals;

struct Session {
  std::string token, candidate, status = "FAILED", error;
  uint64_t sequence = 0;
  uint32_t remaining = std::numeric_limits<uint32_t>::max();
  bool terminal = false;
  rclcpp::Client<Step>::SharedPtr client;
  std::shared_ptr<Handle> handle;
};

// Long-running requests return RUNNING. No nested spin or sleep inside tick().
class Capability : public BT::StatefulActionNode {
 public:
  Capability(const std::string& name, const BT::NodeConfig& config,
             std::shared_ptr<Session> session) : StatefulActionNode(name, config), s_(session) {}
  static BT::PortsList providedPorts() { return {BT::InputPort<std::string>("command")}; }
  BT::NodeStatus onStart() override {
    if (s_->terminal) return BT::NodeStatus::FAILURE;
    command_ = getInput<std::string>("command").value();
    if (command_ == "pick" && s_->remaining == 0) return BT::NodeStatus::SUCCESS;
    auto feedback = std::make_shared<Run::Feedback>(); feedback->stage = command_;
    s_->handle->publish_feedback(feedback);
    if (!s_->client->service_is_ready()) {
      s_->error = "Capability service unavailable"; s_->terminal = true;
      return BT::NodeStatus::FAILURE;
    }
    auto request = std::make_shared<Step::Request>();
    request->session_id = s_->token; request->sequence = s_->sequence++;
    request->command = command_; request->detection_id = command_ == "pick" ? s_->candidate : "";
    auto sent = s_->client->async_send_request(request);
    future_ = sent.future.share(); request_id_ = sent.request_id;
    started_ = std::chrono::steady_clock::now();
    return BT::NodeStatus::RUNNING;
  }
  BT::NodeStatus onRunning() override {
    if (future_.wait_for(0ms) != std::future_status::ready) {
      if (std::chrono::steady_clock::now()-started_ > 180s) {
        s_->error = "Capability timed out; physical result requires reconciliation";
        s_->terminal = true;
        s_->client->remove_pending_request(request_id_);
        return BT::NodeStatus::FAILURE;
      }
      return BT::NodeStatus::RUNNING;
    }
    try {
      const auto reply = future_.get(); s_->remaining = reply->remaining;
      if (command_ == "select") s_->candidate = reply->detection_id;
      s_->error = reply->reason;
      if (reply->status == "SUCCESS" || reply->status == "DONE") return BT::NodeStatus::SUCCESS;
      if (reply->status != "RETRY") { s_->terminal = true; s_->status = reply->status; }
    } catch (const std::exception& e) { s_->error = e.what(); s_->terminal = true; }
    return BT::NodeStatus::FAILURE;
  }
  void onHalted() override {
    // This only discards the response wait. The Python owner cancels/drains the
    // actual skill before releasing the workstation. Services aren't cancellable.
    s_->client->remove_pending_request(request_id_);
  }
 private:
  std::shared_ptr<Session> s_;
  std::string command_;
  rclcpp::Client<Step>::SharedFuture future_;
  int64_t request_id_ = 0;
  std::chrono::steady_clock::time_point started_;
};

class Executor : public rclcpp::Node {
 public:
  Executor() : Node("produce_order_bt_executor") {
    client_ = create_client<Step>("/produce_orders/internal/step");
    server_ = rclcpp_action::create_server<Run>(this, "/produce_orders/internal/run_tree",
      [this](auto, auto goal) {
        bool expected = false;
        if (goal->session_id.empty() || goal->max_attempts < 1 || goal->max_attempts > 10 ||
            !busy_.compare_exchange_strong(expected, true)) return rclcpp_action::GoalResponse::REJECT;
        return rclcpp_action::GoalResponse::ACCEPT_AND_EXECUTE;
      }, [](auto) { return rclcpp_action::CancelResponse::ACCEPT; },
      [this](auto handle) {
        if (worker_.joinable()) worker_.join();
        worker_ = std::thread([this, handle]() { execute(handle); });
      });
  }
  ~Executor() override { if (worker_.joinable()) worker_.join(); }
 private:
  void execute(std::shared_ptr<Handle> handle) {
    auto result = std::make_shared<Run::Result>();
    auto session = std::make_shared<Session>();
    session->token = handle->get_goal()->session_id; session->client = client_; session->handle = handle;
    try {
      BT::BehaviorTreeFactory factory;
      factory.registerNodeType<Capability>("Capability", session);
      factory.registerBehaviorTreeFromFile(ament_index_cpp::get_package_share_directory("produce_order_bt")+"/trees/item.xml");
      auto blackboard = BT::Blackboard::create();
      blackboard->set("attempts", int(handle->get_goal()->max_attempts));
      auto run_tree = [&](const std::string& name) {
        auto tree = factory.createTree(name, blackboard);
        BT::NodeStatus state = BT::NodeStatus::RUNNING;
        while (rclcpp::ok() && !handle->is_canceling() && state == BT::NodeStatus::RUNNING) {
          state = tree.tickOnce(); std::this_thread::sleep_for(20ms);
        }
        tree.haltTree();
        return state == BT::NodeStatus::SUCCESS && !handle->is_canceling();
      };
      bool good = true;
      // Bound the number of completed item cycles as well as per-item retries.
      unsigned cycles = 0;
      while (good && session->remaining != 0 && cycles++ < 1000) good = run_tree("Item");
      if (good && session->remaining == 0 && run_tree("Audit")) result->status = "COMPLETED";
      else { result->status = session->status; result->error = session->error.empty() ? "Bounded behavior-tree execution failed" : session->error; }
    } catch (const std::exception& e) { result->status = "FAILED"; result->error = e.what(); }
    if (handle->is_canceling()) { result->status = "CANCELLED"; handle->canceled(result); }
    else if (result->status == "COMPLETED" || result->status == "PAUSED") handle->succeed(result);
    else handle->abort(result);
    busy_ = false;
  }
  std::atomic_bool busy_{false};
  std::thread worker_;
  rclcpp::Client<Step>::SharedPtr client_;
  rclcpp_action::Server<Run>::SharedPtr server_;
};

int main(int argc, char** argv) {
  rclcpp::init(argc, argv);
  auto node = std::make_shared<Executor>();
  rclcpp::executors::MultiThreadedExecutor executor;
  executor.add_node(node); executor.spin();
  rclcpp::shutdown(); return 0;
}
