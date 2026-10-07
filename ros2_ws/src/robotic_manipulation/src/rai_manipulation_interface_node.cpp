/* Copyright (C) 2025 Robotec.AI
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *         http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#include "robotic_manipulation/rai_manipulation_interface_node.h"

#include <algorithm>
#include <cmath>

namespace
{
struct BusyReset
{
  std::atomic_bool & busy;
  ~BusyReset() {busy.store(false);}
};
}  // namespace

RaiManipulationInterfaceNode::RaiManipulationInterfaceNode()
: m_node(rclcpp::Node::make_shared("state_controller"))
{
  m_node->set_parameter(rclcpp::Parameter("use_sim_time", true));
}

RaiManipulationInterfaceNode::~RaiManipulationInterfaceNode()
{
  if (m_actionThread.joinable()) {
    m_actionThread.join();
  }
}

bool RaiManipulationInterfaceNode::RunMove(
  ArmController & arm, const std::string & step,
  const geometry_msgs::msg::PoseStamped & target_pose,
  bool initial_gripper_state, bool final_gripper_state,
  const std::function<void(const std::string &)> & publish_stage,
  const std::function<bool()> & cancel_requested, std::string & error)
{
  const auto & pose = target_pose.pose;
  if (step != "legacy" && step != "grab" && step != "lift" &&
    step != "place" && step != "retreat" && step != "smoke")
  {
    error = "Unsupported motion step";
    return false;
  }
  if (target_pose.header.frame_id.empty() ||
    !std::isfinite(pose.position.x) || !std::isfinite(pose.position.y) ||
    !std::isfinite(pose.position.z))
  {
    error = "Invalid target pose or reference frame";
    return false;
  }
  auto check_cancel = [&]() {
      if (cancel_requested()) {
        error = "Canceled at motion stage boundary; workcell recovery may be required";
        return true;
      }
      return false;
    };
  if (check_cancel()) return false;

  arm.SetReferenceFrame(target_pose.header.frame_id);
  const auto current = arm.GetEffectorPose();
  if (current.size() < 3) {
    error = "Current end-effector pose is unavailable";
    return false;
  }
  if ((step == "lift" || step == "retreat") &&
    std::hypot(pose.position.x - current[0], pose.position.y - current[1]) >= 0.02)
  {
    error = "Local lift/retreat requires an unchanged XY target (within 2 cm)";
    return false;
  }
  publish_stage("INITIAL_GRIPPER");
  if (initial_gripper_state ? !arm.Open() : !arm.Close()) {
    error = "Initial gripper command failed";
    return false;
  }
  if (check_cancel()) return false;

  auto above_current = current;
  // The order route is planned through the real centre divider at the lowest
  // clearance that still protects stock and bin rims. A fixed 0.4 m waypoint
  // simply passed over the divider and never exercised obstacle avoidance.
  auto safe_z = [step](double z) {
      return step == "legacy" ? std::clamp(z + 0.1, 0.3, 0.4) : std::max(z, 0.3);
    };
  above_current[2] = safe_z(above_current[2]);
  auto above_target = pose;
  above_target.position.z = safe_z(pose.position.z);

  // Keep the legacy service unchanged. For order Actions, only the transfer
  // between clearance poses is planned freely; contact motions stay Cartesian.
  if (step == "legacy") {
    publish_stage("MOVING_TO_TARGET");
    if (!arm.MoveThroughWaypoints(
        {arm.CalculatePose(above_current[0], above_current[1], above_current[2]),
          above_target, pose}))
    {
      error = "Manipulator path planning or execution failed";
      return false;
    }
  } else if (step == "lift" || step == "retreat")
  {
    publish_stage("LOCAL_CARTESIAN");
    if (!arm.MoveThroughWaypoints({pose})) {
      error = "Local lift or retract could not be completed";
      return false;
    }
  } else {
    if (above_current[2] > current[2] + 0.005) {
      publish_stage("CLEARING_WORKSPACE");
      auto rise_pose = pose;
      rise_pose.position.x = current[0];
      rise_pose.position.y = current[1];
      rise_pose.position.z = above_current[2];
      if (!arm.MoveThroughWaypoints({rise_pose}))
      {
        error = "Could not rise to the transit clearance pose";
        return false;
      }
    }
    if (check_cancel()) return false;
    ArmController::ArmPlan plan;
    publish_stage("PLANNING_TRANSIT");
    if (!arm.PlanFreeSpaceToPose(above_target, plan)) {
      error = "MoveIt could not plan a collision-free transit route";
      return false;
    }
    if (check_cancel()) return false;
    publish_stage("EXECUTING_TRANSIT");
    if (!arm.ExecutePlan(plan)) {
      error = "MoveIt transit route did not execute successfully";
      return false;
    }
    if (check_cancel()) return false;
    if (std::abs(above_target.position.z - pose.position.z) > 0.005) {
      publish_stage("FINAL_APPROACH");
      if (!arm.MoveThroughWaypoints({pose})) {
        error = "Local Cartesian approach failed";
        return false;
      }
    }
  }
  if (check_cancel()) return false;

  publish_stage("FINAL_GRIPPER");
  if (final_gripper_state ? !arm.Open() : !arm.Close()) {
    error = "Final gripper command failed";
    return false;
  }
  if (check_cancel()) return false;

  // The arm has already reached pose. A second, identical one-point path is
  // a no-op that MoveIt may legitimately report as a 0% Cartesian path.
  if (check_cancel()) return false;
  publish_stage("DONE");
  return true;
}

void RaiManipulationInterfaceNode::Initialize(ArmController & arm)
{
  m_arm = &arm;
  m_startingPose = arm.CaptureJointValues();
  auto logger = m_node->get_logger();
  RCLCPP_INFO(logger, "Current gripper state: %d", arm.GetGripper());

  m_moveToService = m_node->create_service<rai_interfaces::srv::ManipulatorMoveTo>(
    "/manipulator_move_to",
    [&arm, this](
      std::shared_ptr<rai_interfaces::srv::ManipulatorMoveTo::Request> const request,
      std::shared_ptr<rai_interfaces::srv::ManipulatorMoveTo::Response> response) {
      response->success = false;
      if (m_motionBusy.exchange(true)) return;
      BusyReset reset{m_motionBusy};
      std::string error;
      try {
        response->success = RunMove(
          arm, "legacy", request->target_pose, request->initial_gripper_state,
          request->final_gripper_state, [](const std::string &) {},
          []() {return false;}, error);
      } catch (const std::exception & exc) {
        error = exc.what();
      }
      if (!response->success) {
        RCLCPP_ERROR(m_node->get_logger(), "Legacy manipulator service failed: %s", error.c_str());
      }
    });

  m_motionAction = rclcpp_action::create_server<ExecuteMotion>(
    m_node, "/produce_orders/execute_motion",
    [this](const rclcpp_action::GoalUUID &, std::shared_ptr<const ExecuteMotion::Goal> goal) {
      const auto & step = goal->step;
      const bool supported = step == "grab" || step == "lift" ||
        step == "place" || step == "retreat" || step == "smoke";
      // True means open. Reject inconsistent phase/gripper combinations
      // before any physical command; smoke is an empty-hand diagnostic.
      const bool initially_open = step == "grab" || step == "retreat" || step == "smoke";
      const bool finally_open = step == "place" || step == "retreat" || step == "smoke";
      if (!supported || goal->target_pose.header.frame_id.empty() ||
        goal->initial_gripper_state != initially_open ||
        goal->final_gripper_state != finally_open)
      {
        return rclcpp_action::GoalResponse::REJECT;
      }
      bool expected = false;
      if (!m_motionBusy.compare_exchange_strong(expected, true)) {
        return rclcpp_action::GoalResponse::REJECT;
      }
      return rclcpp_action::GoalResponse::ACCEPT_AND_EXECUTE;
    },
    [](const std::shared_ptr<MotionGoalHandle>) {
      return rclcpp_action::CancelResponse::ACCEPT;
    },
    [this](const std::shared_ptr<MotionGoalHandle> goal_handle) {
      if (m_actionThread.joinable()) m_actionThread.join();
      m_actionThread = std::thread(&RaiManipulationInterfaceNode::ExecuteAction, this, goal_handle);
    });

  m_resetService = m_node->create_service<std_srvs::srv::Trigger>(
    "/reset_manipulator",
    [&arm, this](
      [[maybe_unused]] std::shared_ptr<std_srvs::srv::Trigger::Request> const request,
      std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
      if (m_motionBusy.exchange(true)) {
        response->success = false;
        response->message = "Manipulator is busy";
        return;
      }
      BusyReset reset{m_motionBusy};
      response->success = arm.SetJointValues(m_startingPose);
    });
  RCLCPP_INFO(logger, "Manipulator service and /produce_orders/execute_motion Action are ready");
}

void RaiManipulationInterfaceNode::ExecuteAction(
  const std::shared_ptr<MotionGoalHandle> & goal_handle)
{
  BusyReset reset{m_motionBusy};
  auto result = std::make_shared<ExecuteMotion::Result>();
  const auto goal = goal_handle->get_goal();
  auto feedback = [goal_handle, goal](const std::string & stage) {
      auto message = std::make_shared<ExecuteMotion::Feedback>();
      message->stage = goal->step + ":" + stage;
      goal_handle->publish_feedback(message);
    };
  try {
    result->success = RunMove(
      *m_arm, goal->step, goal->target_pose, goal->initial_gripper_state,
      goal->final_gripper_state, feedback,
      [goal_handle]() {return goal_handle->is_canceling();}, result->error);
  } catch (const std::exception & exc) {
    result->error = exc.what();
  }
  if (goal_handle->is_canceling()) {
    result->success = false;
    goal_handle->canceled(result);
  } else if (result->success) {
    goal_handle->succeed(result);
  } else {
    goal_handle->abort(result);
  }
}

void RaiManipulationInterfaceNode::Spin()
{
  m_executor.add_node(m_node);
  m_executor.spin();
}
