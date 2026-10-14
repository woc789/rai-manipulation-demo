"""Business tools: read truth, propose changes, never bypass resource admission."""

import hashlib
import json
import time
import os

from .order import Order, OrderLine, validate_order


TOOL_DESCRIPTIONS = {
    "query_orders": "Query actual order queue, progress and waiting reasons.",
    "query_inventory": "Query observed candidates and uncertainty, not guaranteed physical stock.",
    "propose_fulfillment_plan": "Explain deterministic queue eligibility; this does not dispatch or change priority.",
    "request_order_change": "Create an operator approval request for an unstarted order; never apply directly.",
    "request_replenishment": "Request operator replenishment; never invent or spawn stock.",
    "explain_task_status": "Query factual workcell/order state and permitted next steps.",
}


def order_version(order):
    value = [order.order_id, order.zone, [(line.sku, line.quantity) for line in order.lines]]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()[:16]


class BusinessCoordinator:
    def __init__(self, server):
        self.server = server
        self.boot_id = __import__("uuid").uuid4().hex
        if server.store.quarantine_interrupted_maintenance():
            server._startup_review = True
            server._state, server._state_reason = "RECOVERY_REQUIRED", "上次维护结果未知；请核对现场，不能自动重复维护"

    def call(self, command, args):
        s = self.server
        if not isinstance(args, dict):
            raise ValueError("Arguments must be an object")
        if command == "ask":
            return self.ask(args.get("question", ""))
        with s._lock:
            if command == "query_orders":
                return dict(orders=s.queue.snapshot())
            if command == "query_inventory":
                snapshot = s.workstation.inventory.snapshot()
                if args.get("sku"):
                    snapshot["items"] = [row for row in snapshot["items"] if row["sku"] == args["sku"]]
                return snapshot
            if command in ("explain_task_status", "propose_fulfillment_plan"):
                jobs = s.queue.snapshot()
                if args.get("order_id"):
                    jobs = [job for job in jobs if job["order_id"] == args["order_id"]]
                return dict(state=s._state, reason=s._state_reason, orders=jobs,
                            inventory=s.workstation.inventory.summary(), bins=s.bin_manager.snapshot(),
                            policy="priority descending, FIFO ties; only switch after verified placement and retreat",
                            allowed_actions=["query_orders", "query_inventory", "refresh_inventory", "request_replenishment"],
                            recovery_requires_operator=s._state == "RECOVERY_REQUIRED")
            if command == "list_requests":
                return dict(requests=s.store.requests())
            if command in ("request_order_change", "request_replenishment", "request_scene_reset"):
                body = dict(kind=command, boot_id=self.boot_id, arguments=args)
                if command == "request_order_change":
                    job = s.queue.jobs.get(args.get("order_id"))
                    if not job or job.completed or job.state not in ("QUEUED", "WAITING_STOCK", "WAITING_BIN"):
                        raise ValueError("Only an unstarted queued order can be changed")
                    quantities = args.get("quantities", {})
                    if not isinstance(quantities, dict) or not quantities or any(type(n) is not int or n < 0 for n in quantities.values()):
                        raise ValueError("quantities must contain nonnegative integer counts")
                    changed = Order(job.order.order_id, job.order.zone,
                                    tuple(OrderLine(sku, n) for sku, n in quantities.items() if n))
                    validate_order(changed, s.config)
                    body["expected_version"] = order_version(job.order)
                elif command == "request_replenishment":
                    # Full-table replenishment is the existing operation; requested
                    # counts are advice, never used as inventory measurements.
                    body["scope"] = "replace remaining table source; preserve order bins"
                elif not getattr(s, "_startup_review", False):
                    raise ValueError("Scene-reset reconciliation is only for a restored unverified scene")
                return dict(request=s.store.create_request(body), applied=False)
            if command in ("approve_request", "reject_request"):
                request = s.store.request(args.get("request_id", ""))
                retryable = bool(request and request["state"] == "APPROVED"
                    and request.get("execution_state") == "REJECTED")
                if (not request or request["expires"] < time.time() or request["boot_id"] != self.boot_id
                    or (request["state"] != "PENDING" and not (command == "approve_request" and retryable))):
                    raise ValueError("Request missing, expired, already handled, or from another process")
                if command == "reject_request":
                    s.store.resolve_request(request["request_id"], "REJECTED")
                    return dict(applied=False, state="REJECTED")
                if request["kind"] == "request_order_change":
                    job = s.queue.jobs[request["arguments"]["order_id"]]
                    if job.completed or job.state not in ("QUEUED", "WAITING_STOCK", "WAITING_BIN") or order_version(job.order) != request["expected_version"]:
                        raise ValueError("Order changed or started; create a new proposal")
                    quantities = request["arguments"]["quantities"]
                    changed = Order(job.order.order_id, job.order.zone, tuple(OrderLine(k, n) for k, n in quantities.items() if n))
                    validate_order(changed, s.config)
                    before = (job.order, job.total, job.state, job.reason, job.missing, s._scan_requested)
                    # Both manager and ledger locks remain held. A failed SQLite
                    # commit rolls back all in-memory changes before observers run.
                    with s.workstation.inventory.transaction():
                        try:
                            s.workstation.inventory.release_order(job.order.order_id)
                            job.order, job.total = changed, changed.total_items
                            job.state, job.reason, job.missing = "QUEUED", "操作员已确认修改，重新检查资源", {}
                            s._scan_requested = True
                            s._persist_locked(resolution=(request["request_id"], "APPROVED"))
                        except BaseException:
                            job.order, job.total, job.state, job.reason, job.missing, s._scan_requested = before
                            raise
                    return dict(applied=True, state="QUEUED")
                if s._busy or s._dispatch_order_id:
                    raise ValueError("Wait for the current operation before approving scene maintenance")
                if request["kind"] == "request_scene_reset":
                    if args.get("confirmation") != "RESET_SCENE":
                        raise ValueError("Explicit confirmation RESET_SCENE required; old physical bins will be discarded")
                    if not s._startup_review:
                        raise ValueError("Scene review was already resolved")
                    maintenance = "recover"
                elif request["kind"] == "request_replenishment":
                    if s._startup_review:
                        raise ValueError("Resolve restored scene before replenishment")
                    maintenance = "replenish"
                else:
                    raise ValueError("Unknown maintenance kind")
                identifier = request["request_id"]
                s.store.admit_maintenance(identifier)
            else:
                raise ValueError("Unknown business command")
        from std_srvs.srv import Trigger
        response = Trigger.Response()
        try:
            if hasattr(s, "start_maintenance"):
                s.start_maintenance(maintenance, identifier, response)
            elif maintenance == "recover":
                s.recover_callback(Trigger.Request(), response)
            else:
                s.replenish_callback(Trigger.Request(), response)
        except Exception as exc:
            s.store.execution_result(identifier, "UNKNOWN", str(exc))
            raise
        if not response.success:
            s.store.execution_result(identifier, "REJECTED", response.message, expected=("ADMITTING",))
            return dict(applied=False, error=response.message, state="MAINTENANCE_REJECTED",
                        execution_state="REJECTED", retryable=True)
        # A fast worker may already have finished; never downgrade SUCCEEDED.
        s.store.execution_result(identifier, "RUNNING", expected=("ADMITTING",))
        current = s.store.request(identifier)
        return dict(applied=True, message=response.message, execution_state=current.get("execution_state"))

    def ask(self, question):
        """Dispatch business reasoning to the independent Agent, never call a model here."""
        if not isinstance(question, str) or not question.strip() or len(question) > 2000:
            raise ValueError("Provide a question of 1–2000 characters")
        import time
        from rai.communication.ros2.ros_async import get_future_result
        from produce_order_interfaces.action import ExecuteBusiness
        from rclpy.action import ActionClient
        from rclpy.callback_groups import ReentrantCallbackGroup
        s = self.server
        if not hasattr(self, "_agent_client"):
            self._agent_client = ActionClient(s, ExecuteBusiness, "/produce_agent/business",
                callback_group=ReentrantCallbackGroup())
        client = self._agent_client
        if not client.wait_for_server(timeout_sec=5):
            raise ValueError("Business Agent service unavailable")
        gateway = s._business_gateway
        accepted = get_future_result(client.send_goal_async(ExecuteBusiness.Goal(
            question=question, mcp_url=gateway.url, mcp_token=gateway.token)), timeout_sec=15)
        if accepted is None or not accepted.accepted:
            raise ValueError("Business Agent busy or admission timed out; do not resubmit proposals blindly")
        future = accepted.get_result_async()
        deadline = time.monotonic() + 180
        while not future.done():
            if time.monotonic() > deadline:
                accepted.cancel_goal_async()
                raise ValueError("Business Agent timed out; check existing approval requests before retrying")
            time.sleep(.02)
        reply = future.result().result
        value = json.loads(reply.result_json)
        if reply.status != "COMPLETED":
            raise ValueError(value.get("error", "Business Agent failed"))
        return value
