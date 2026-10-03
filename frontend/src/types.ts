export interface CheckinBalance {
  code: string; quota_before: number | null; quota_after: number | null; delta: number | null; earned: number | null;
  used_before: number | null; used_after: number | null;
  created: number; balance_source: 'live' | 'legacy';
}
export interface Account {
  id: string; username: string; validity: string; message: string; has_password: boolean; has_result: boolean;
  credential_source: 'password' | 'session' | 'missing';
  last_validated: number | null; last_extracted: number | null; last_checkin: number | null;
  checkin_status: string | null; quota: number | null; created: number; updated: number; note: string; position: number; last_balance: CheckinBalance | null;
}
export interface Selection { ids?: string[]; all_matching?: boolean; validity?: string; exclude_ids?: string[]; only_extracted?: boolean; }
export interface PageData<Item> { items: Item[]; total: number; page: number; limit: number; }
export interface Job {
  id: string; account_id: string | null; proxy_id: string | null; kind: string; status: string; source: string;
  message: string; created: number; started: number | null; finished: number | null; username?: string;
}
export interface JobStatus {
  id: string; account_id: string | null; proxy_id: string | null; kind: string; status: string; message: string; finished: number | null;
  username: string | null; validity: string | null; quota: number | null; proxy_name: string | null; balance: CheckinBalance | null;
}
export interface Dashboard {
  accounts: { total: number; valid: number; invalid: number; extracted: number };
  jobs: { pending: number; running: number }; next_run: number | null; paused: boolean; pause_reason: string; timezone: string;
  proxy_mode: 'pool' | 'direct'; enabled_proxies: number; storage_error: boolean; max_concurrency: number; checkin_concurrency: number;
}
export interface QueueSettings { max_concurrency: number; checkin_concurrency: number; }
export interface QueueData extends PageData<Job>, QueueSettings {
  paused: boolean; pause_reason: string;
  counts: { queue_pending: number; queue_running: number; checkin_pending: number; checkin_running: number };
}
export interface Schedule {
  id: string; name: string; interval_minutes: number; enabled: boolean; next_run: number;
  last_run: number | null; created: number; account_count: number; account_ids?: string[];
}
export interface ProxyNode {
  id: string; name: string; enabled: boolean; scheme: 'auto' | 'http' | 'socks5' | 'ssh'; host: string; port: number;
  username: string; has_password: boolean; status: string; message: string; latency: number | null;
  tested_at: number | null; candidate_fingerprint: string | null; trusted: boolean;
}
export interface SiteBranding { site_name: string; site_icon_text: string; }
export interface RuntimeSettings extends SiteBranding {
  proxy_mode: 'pool' | 'direct'; connect_timeout: number; login_timeout: number; account_gap: number;
  log_retention_days: number; queue_retention_days: number; log_cleanup_hours: number; log_cleanup_enabled: boolean; timezone: string;
  auto_checkin: boolean; auto_checkin_interval_minutes: number; auto_reextract: boolean;
  max_concurrency: number; checkin_concurrency: number; stats_retention_days: number;
}
export interface SettingsData { settings: RuntimeSettings; version: string; script_revision: string; last_log_cleanup: number | null; next_log_cleanup: number | null; auto_schedule_id: string | null; }
export interface CleanupResult { removed: number; logs: number; jobs: number; checkins: number; }
export interface HistoryResult { removed: number; protected: number; }
export interface CheckinEntry extends CheckinBalance { id: string; account_id: string; job_id: string | null; username: string | null; }
export interface LogEntry {
  id: number; job_id: string | null; account_id: string | null; kind: string; level: string; message: string; created: number; username: string | null; category: string;
}
export interface ActionResult { queued: number; skipped?: number; filtered_count?: number; needs_password?: string[]; needs_password_count?: number; job_ids?: string[]; }
export interface StatsAccount {
  account_id: string; username: string; note: string; quota: number | null; validity: string; signed: number; already: number; failed: number; uncertain: number;
  earned: number; last: number | null; last_code: string | null; last_balance: CheckinBalance | null;
}
export interface Stats {
  range: 'day' | 'week' | 'month'; since: number; until: number; timezone: string; attempts: number; signed: number; already: number; failed: number; uncertain: number; earned: number;
  balance_total: number; balance_accounts: number; accounts_total: number; legacy_records: number; unmeasured: number;
  series: { day: string; signed: number; already: number; failed: number; uncertain: number; earned: number }[]; accounts: StatsAccount[]; stats_retention_days: number;
}
declare global { const __APP_VERSION__: string; }
