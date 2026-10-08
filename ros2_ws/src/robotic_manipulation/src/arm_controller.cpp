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

#include "robotic_manipulation/arm_controller.h"
#include "robotic_manipulation/joint_state_guard.h"

#include <numbers>
#include <rosgraph_msgs/msg/clock.hpp>
#include <control_msgs/action/gripper_command.hpp>
#include <chrono>
#include <functional>
#include <thread>
#include <future>

void ArmController::Initialize()
{
  m_node = rclcpp::Node::make_shared("arm_controller");
  m_node->set_parameter(rclcpp::Parameter("use_sim_time", true));

  ActionName = "panda_hand_controller/vacuum_gripper_cmd";

  WaitForClockMessage();

  m_executor.add_node(m_node);
  m_spinner = std::thread([this]() { m_executor.spin(); });

  m_pandaArm =
    std::make_shared<moveit::planning_interface::MoveGroupInterface>(m_node, "panda_arm");
  m_hand = std::make_shared<moveit::planning_interface::MoveGroupInterface>(m_node, "hand");
  m_gripperClient = rclcpp_action::create_client<GripperCommand>(m_node, ActionName);

  m_pandaArm->setMaxVelocityScalingFactor(1.0);
  m_pandaArm->setMaxAccelerationScalingFactor(1.0);
  m_pandaArm->setPlanningTime(5.0);
  m_pandaArm->setNumPlanningAttempts(3);
}

ArmController::~ArmController()
{
  m_executor.cancel();
  if (m_spinner.joinable()) {
    m_spinner.join();
  }
}

geometry_msgs::msg::Pose ArmController::CalculatePose(double x, double y, double z, double r)
{
  geometry_msgs::msg::Pose pose;
  pose.position.x = x;
  pose.position.y = y;
  pose.position.z = z;

  auto quat = tf2::Quaternion();
  quat.setEuler(EndEffectorBaseRoll, EndEffectorBasePitch, EndEffectorBaseYaw + r);
  pose.orientation.x = quat.x();
  pose.orientation.y = quat.y();
  pose.orientation.z = quat.z();
  pose.orientation.w = quat.w();

  return pose;
}

bool ArmController::MoveThroughWaypoints(std::vector<geometry_msgs::msg::Pose> const & waypoints)
{
  auto logger = m_node->get_logger();

  m_lastMotionError.clear();
  int constexpr NumTries = 3;
  for (int i = 0; i < NumTries; i++) {
    auto start = WaitForStableState();
    if (!start) return false;
    // Do not rely on move_group's asynchronously updated empty-diff start state.
    SetMeasuredStartState(*start);
    moveit_msgs::msg::RobotTrajectory trajectory;
    const double fraction = m_pandaArm->computeCartesianPath(waypoints, 0.01, trajectory);
    m_pandaArm->setStartStateToCurrentState();
    if (!std::isfinite(fraction) || fraction < 0.999) {
      m_lastMotionError = "Cartesian planning incomplete (fraction " + std::to_string(fraction) + ")";
      RCLCPP_ERROR(logger, "MoveThroughWaypoints: Incomplete Cartesian path (fraction %.3f)", fraction);
      continue;
    }

    if (!CheckTrajectoryStart(trajectory)) {
      RCLCPP_WARN(logger, "Cartesian start changed before execution; re-planning (%d/%d)", i + 1, NumTries);
      continue;
    }
    const auto result = m_pandaArm->execute(trajectory);
    if (result == moveit::core::MoveItErrorCode::SUCCESS) {
      return static_cast<bool>(WaitForStableState());
    }
    m_lastMotionError = "Cartesian execution rejected/failed (MoveIt code " +
      std::to_string(result.val) + "); no automatic execution retry";
    RCLCPP_ERROR(logger, "MoveThroughWaypoints: Trajectory execution failed; state is uncertain");
    return false;
  }

  RCLCPP_ERROR(
    logger, "MoveThroughWaypoints: Failed to execute trajectory after %d tries", NumTries);
  return false;
}

moveit::core::RobotStatePtr ArmController::WaitForStableState()
{
  motion_guard::StabilityWindow window;
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(3);
  while (rclcpp::ok() && std::chrono::steady_clock::now() < deadline) {
    // getCurrentState waits for feedback at least as new as this request's ROS time.
    const double stamp = m_node->get_clock()->now().seconds();
    auto state = m_pandaArm->getCurrentState(0.4);
    if (state) {
      std::vector<double> positions;
      state->copyJointGroupPositions("panda_arm", positions);
      const double wall = std::chrono::duration<double>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
      if (window.Observe(positions, stamp, wall)) return state;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
  }
  m_lastMotionError = "Fresh, stable arm joint feedback unavailable within 3 seconds";
  RCLCPP_ERROR(m_node->get_logger(), "%s", m_lastMotionError.c_str());
  return nullptr;
}

bool ArmController::CheckTrajectoryStart(const moveit_msgs::msg::RobotTrajectory & trajectory)
{
  const auto & joints = trajectory.joint_trajectory;
  auto current = m_pandaArm->getCurrentState(0.4);
  if (!current || joints.points.empty() || joints.joint_names.empty()) {
    m_lastMotionError = "Trajectory start or fresh measured joint state unavailable";
    return false;
  }
  std::vector<double> measured;
  const auto & variables = current->getRobotModel()->getVariableNames();
  for (const auto & name : joints.joint_names) {
    if (std::find(variables.begin(), variables.end(), name) == variables.end()) {
      m_lastMotionError = "Unknown trajectory joint: " + name;
      return false;
    }
    measured.push_back(current->getVariablePosition(name));
  }
  const double error = motion_guard::MaxError(joints.points.front().positions, measured);
  RCLCPP_INFO(m_node->get_logger(), "Trajectory start max measured joint error: %.6f rad", error);
  // Stricter than MoveIt's unchanged 0.01-rad safety check, not a relaxed tolerance.
  if (error > 0.005) {
    m_lastMotionError = "Trajectory start changed before execution (max error " +
      std::to_string(error) + " rad)";
    return false;
  }
  return true;
}

void ArmController::SetMeasuredStartState(const moveit::core::RobotState & state)
{
  moveit_msgs::msg::RobotState measured;
  // Explicit arm joint values, but a diff: retain carried collision objects from
  // the planning scene rather than replacing them with the monitor's empty list.
  measured.is_diff = true;
  measured.joint_state.name = state.getJointModelGroup("panda_arm")->getVariableNames();
  for (const auto & name : measured.joint_state.name) {
    measured.joint_state.position.push_back(state.getVariablePosition(name));
  }
  m_pandaArm->setStartState(measured);
}

bool ArmController::PlanFreeSpaceToPose(
  const geometry_msgs::msg::Pose & target, ArmPlan & plan)
{
  m_lastMotionError.clear();
  auto start = WaitForStableState();
  if (!start) return false;
  SetMeasuredStartState(*start);
  if (!m_pandaArm->setPoseTarget(target)) {
    RCLCPP_ERROR(m_node->get_logger(), "No IK target for free-space motion");
    m_pandaArm->clearPoseTargets();
    m_pandaArm->setStartStateToCurrentState();
    return false;
  }
  const auto result = m_pandaArm->plan(plan);
  m_pandaArm->setStartStateToCurrentState();
  m_pandaArm->clearPoseTargets();
  if (result != moveit::core::MoveItErrorCode::SUCCESS ||
    plan.trajectory.joint_trajectory.points.empty())
  {
    RCLCPP_ERROR(m_node->get_logger(), "MoveIt could not find a free-space route");
    return false;
  }
  return true;
}

bool ArmController::ExecutePlan(const ArmPlan & plan)
{
  if (!CheckTrajectoryStart(plan.trajectory)) return false;
  if (m_pandaArm->execute(plan) != moveit::core::MoveItErrorCode::SUCCESS) {
    RCLCPP_ERROR(m_node->get_logger(), "Free-space route execution failed");
    return false;
  }
  return static_cast<bool>(WaitForStableState());
}

bool ArmController::Open()
{
  m_hand->setJointValueTarget("panda_finger_joint1", OpenGripperJointValue);
  bool moved = false;
  for (int attempt = 0; attempt < 3; ++attempt) {
    if (m_hand->move() == moveit::core::MoveItErrorCode::SUCCESS) {
      moved = true;
      break;
    }
    RCLCPP_ERROR(m_node->get_logger(), "Failed to open hand (attempt %d/3)", attempt + 1);
  }

  // Use action client to send open command. Small max_effort allows free opening.
  if (!moved || !SendGripperCommand(1.0)) {
    RCLCPP_ERROR(m_node->get_logger(), "Failed to open hand");
    return false;
  }
  gripper.store(true);
  return true;
}

bool ArmController::Close()
{
  m_hand->setJointValueTarget("panda_finger_joint1", 0.02);
  bool moved = false;
  for (int attempt = 0; attempt < 3; ++attempt) {
    if (m_hand->move() == moveit::core::MoveItErrorCode::SUCCESS) {
      moved = true;
      break;
    }
    RCLCPP_ERROR(m_node->get_logger(), "Failed to close hand (attempt %d/3)", attempt + 1);
  }

  // Use action client to send close command. Increase max_effort for gripping.
  if (!moved || !SendGripperCommand(0.0)) {
    RCLCPP_ERROR(m_node->get_logger(), "Failed to close hand");
    return false;
  }
  gripper.store(false);
  return true;
}

bool ArmController::SendGripperCommand(double position)
{
  auto logger = m_node->get_logger();
  if (!m_gripperClient) {
    RCLCPP_ERROR(logger, "Gripper action client not initialized");
    return false;
  }

  // Wait a short time for the action server to be available.
  if (!m_gripperClient->wait_for_action_server(std::chrono::seconds(5))) {
    RCLCPP_ERROR(logger, "Gripper action server '%s' not available", ActionName.c_str());
    return false;
  }

  GripperCommand::Goal goal_msg;
  goal_msg.command.position = position;

  auto goal_handle_future = m_gripperClient->async_send_goal(goal_msg);
  if (goal_handle_future.wait_for(std::chrono::seconds(10)) != std::future_status::ready) {
    RCLCPP_ERROR(logger, "Timed out waiting for gripper goal acceptance");
    return false;
  }

  auto goal_handle = goal_handle_future.get();
  if (!goal_handle) {
    RCLCPP_ERROR(logger, "Goal was rejected by gripper action server");
    return false;
  }

  auto result_future = m_gripperClient->async_get_result(goal_handle);
  if (result_future.wait_for(std::chrono::seconds(30)) != std::future_status::ready) {
    RCLCPP_ERROR(logger, "Timed out waiting for gripper action result");
    m_gripperClient->async_cancel_goal(goal_handle);
    return false;
  }

  auto result = result_future.get();
  return result.code == rclcpp_action::ResultCode::SUCCEEDED;
}

std::vector<double> ArmController::GetEffectorPose()
{
  auto pose = m_pandaArm->getCurrentPose().pose;
  auto rotation =
    tf2::Quaternion(pose.orientation.x, pose.orientation.y, pose.orientation.z, pose.orientation.w);
  tf2::Matrix3x3 m(rotation);
  m.getRPY(pose.orientation.x, pose.orientation.y, pose.orientation.z, 0);
  return {
    pose.position.x,
    pose.position.y,
    pose.position.z,
    pose.orientation.x,
    pose.orientation.y - EndEffectorBasePitch,
    pose.orientation.z - (tf2Radians(180.0) - EndEffectorBaseYaw)};
};

bool ArmController::GetGripper() { return gripper.load(); }

std::vector<double> ArmController::CaptureJointValues()
{
  return m_pandaArm->getCurrentJointValues();
}

bool ArmController::SetJointValues(std::vector<double> const & jointValues)
{
  m_pandaArm->setJointValueTarget(jointValues);
  if (m_pandaArm->move() != moveit::core::MoveItErrorCode::SUCCESS) {
    RCLCPP_ERROR(m_node->get_logger(), "Failed to set joint values");
    return false;
  }
  return true;
}

void ArmController::SetReferenceFrame(std::string const & frame)
{
  m_pandaArm->setPoseReferenceFrame(frame);
}

void ArmController::WaitForClockMessage()
{
  bool clockReceived = false;
  auto qos = rclcpp::QoS(rclcpp::KeepLast(1));
  qos.best_effort();
  auto subscription = m_node->create_subscription<rosgraph_msgs::msg::Clock>(
    "/clock", qos, [&](rosgraph_msgs::msg::Clock::SharedPtr) { clockReceived = true; });
  while (!clockReceived) {
    rclcpp::spin_some(m_node);
  }
  subscription.reset();
}
