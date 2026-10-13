cmake_minimum_required(VERSION 3.8)
project(produce_order_interfaces)

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(std_msgs REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/OrderLine.msg"
  "msg/InventoryItem.msg"
  "msg/InventorySnapshot.msg"
  "srv/ConfirmPickup.srv"
  "srv/SubmitOrder.srv"
  "srv/CancelQueuedOrder.srv"
  "action/ExecuteOrder.action"
  "action/ExecuteMotion.action"
  "action/ExecuteAgent.action"
  "action/RunFulfillment.action"
  "action/ExecuteBusiness.action"
  "srv/BusinessCommand.srv"
  "srv/FulfillmentStep.srv"
  DEPENDENCIES geometry_msgs std_msgs
)

ament_export_dependencies(rosidl_default_runtime)
ament_package()
