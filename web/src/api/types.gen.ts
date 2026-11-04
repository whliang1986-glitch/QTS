// 由 scripts/gen_api_types.py 生成，不要手改（S-02）。
// 金额与数量是十进制字符串；时间是带时区的 UTC ISO 字符串。

export interface AckResponse {
  alert_id: number;
  requested: boolean;
  already: boolean;
}

export interface AlertList {
  alerts: AlertView[];
}

export interface AlertView {
  alert_id: number;
  key: string;
  level: "P0" | "P1" | "P2" | "P3";
  code: string;
  text: Record<string, string>;
  raised_at: string;
  acked_by: string | null;
  acked_at: string | null;
  resolved_at: string | null;
}

export interface AuditEntry {
  seq: number;
  at: string;
  action: string;
  actor: string;
  detail: Record<string, string>;
}

export interface AuditList {
  entries: AuditEntry[];
}

export interface CommandCatalog {
  commands: CommandInfo[];
}

export interface CommandInfo {
  name: string;
  tier: Tier;
  params: string[];
  summary: string;
}

export interface CommandRequest {
  request_id: string;
  name: string;
  params?: Record<string, string>;
  confirmation_id?: string | null;
}

export interface CommandResponse {
  request_id: string;
  ok: boolean;
  replayed: boolean;
  detail: Record<string, unknown>;
}

export interface ConfirmationRequest {
  name: string;
  params?: Record<string, string>;
}

export interface ConfirmationResponse {
  confirmation_id: string;
  name: string;
  expires_at: string;
}

export interface ErrorResponse {
  error: string;
  retry_same_request_id: boolean;
}

export type EventKind = "RISK_APPROVED" | "RISK_REJECTED" | "SUBMIT_SENT" | "BROKER_ACK" | "BROKER_REJECTED" | "SUBMIT_TIMEOUT" | "PARTIAL_FILL" | "FULL_FILL" | "CANCEL_REQUESTED" | "CANCEL_CONFIRMED" | "CANCEL_REJECTED" | "BROKER_CANCELLED" | "EXPIRED" | "BROKER_STATUS_OBSERVED";

export interface ExecutionView {
  execution_id: string;
  symbol: string;
  side: Side;
  quantity: string;
  price: string;
  commission: string;
  executed_at: string;
}

export interface HaltView {
  halted: boolean;
  reason: string | null;
  since: string | null;
  set_by: string | null;
}

export interface HistoryEntry {
  kind: EventKind;
  status: OrderStatus;
  occurred_at: string;
}

export type Mode = "RESEARCH" | "BACKTEST" | "PAPER" | "ASSISTED_LIVE" | "LIMITED_AUTO_LIVE" | "HALT";

export type OrderClass = "OPEN" | "REDUCE" | "PROTECTIVE_STOP";

export interface OrderDetail {
  order: OrderView;
  history: HistoryEntry[];
  executions: ExecutionView[];
}

export interface OrderEventView {
  seq: number;
  intent_id: string;
  kind: EventKind;
  status: OrderStatus;
  occurred_at: string;
}

export interface OrderList {
  orders: OrderView[];
}

export type OrderStatus = "CREATED" | "RISK_APPROVED" | "REJECTED_BY_RISK" | "SUBMITTING" | "SUBMITTED" | "PARTIALLY_FILLED" | "CANCEL_PENDING" | "UNKNOWN" | "FILLED" | "CANCELLED" | "REJECTED" | "EXPIRED";

export type OrderType = "LIMIT" | "STOP";

export interface OrderView {
  intent_id: string;
  created_at: string;
  strategy_id: string;
  strategy_version: string;
  symbol: string;
  side: Side;
  quantity: string;
  order_type: OrderType;
  limit_price: string | null;
  stop_price: string | null;
  order_class: OrderClass;
  time_in_force: TimeInForce;
  expires_at: string | null;
  status: OrderStatus | null;
  updated_at: string | null;
}

export interface RevokeRequest {
  reason: string;
}

export interface RevokeResponse {
  token_id: string;
  revoked: boolean;
}

export type Side = "BUY" | "SELL";

export interface StatusView {
  mode: Mode | null;
  halt: HaltView | null;
  changed_at: string | null;
  changed_by: string | null;
}

export interface StreamAuth {
  type: "auth";
  token: string;
  after?: number | null;
}

export interface StreamError {
  type: "error";
  error: string;
}

export interface StreamHello {
  type: "hello";
  name: string;
  role: string;
  cursor: number;
}

export interface StreamOrderEvent {
  type: "order_event";
  event: OrderEventView;
}

export interface StreamStatus {
  type: "status";
  status: StatusView;
}

export type Tier = "control" | "high";

export type TimeInForce = "DAY" | "GTD" | "GTC";

export interface WhoAmI {
  name: string;
  role: string;
}

/** /v1/stream 服务端推送的消息 */
export type StreamMessage = StreamHello | StreamStatus | StreamOrderEvent | StreamError;
