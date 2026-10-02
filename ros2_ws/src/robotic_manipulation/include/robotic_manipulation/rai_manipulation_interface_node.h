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

#pragma once

#include <std_srvs/srv/trigger.hpp>
#include <rclcpp_action/rclcpp_action.hpp>
#include <atomic>
#include <functional>
#include <thread>

#include "rai_interfaces/srv/manipulator_move_to.hpp"
#include "produce_order_interfaces/action/execute_motion.hpp"
#include "robotic_manipulation/arm_controller.h"

class RaiManipulationInterfaceNode
{
public:
  RaiManipulationInterfaceNode();
  ~RaiManipulationInterfaceNode();

  void Initialize(ArmController & arm);
  void Spin();

private:
  using ExecuteMotion = produce_order_interfaces::action::ExecuteMotion;
  using MotionGoalHandle = rclcpp_action::ServerGoalHandle<ExecuteMotion>;

  bool RunMove(
    ArmController & arm, const geometry_msgs::msg::PoseStamped & target_pose,
    bool initial_gripper_state, bool final_gripper_state,
    const std::function<void(const std::string &)> & publish_stage,
    const std::function<bool()> & cancel_requested, std::string & error);
  void ExecuteAction(const std::shared_ptr<MotionGoalHandle> & goal_handle);

  rclcpp::Node::SharedPtr m_node;
  rclcpp::Service<rai_interfaces::srv::ManipulatorMoveTo>::SharedPtr m_moveToService;
  rclcpp_action::Server<ExecuteMotion>::SharedPtr m_motionAction;
  rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr m_resetService;

  rclcpp::executors::SingleThreadedExecutor m_executor;
  std::atomic_bool m_motionBusy{false};
  std::thread m_actionThread;
  ArmController * m_arm{nullptr};

  std::vector<double> m_startingPose;
};
