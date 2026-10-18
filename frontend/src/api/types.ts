export type Variant = "desktop" | "mobile";
export type Entity = { id: string; revision: number };
export interface Tag extends Entity {
  name: string;
  count?: number;
}
export interface Folder extends Entity {
  name: string;
  parent_id: string | null;
  count?: number;
  bookmark_count?: number;
  children?: Folder[];
  path?: string;
}
export interface Snapshot {
  id?: string;
  version_id: string;
  variant?: Variant;
  viewport_width: number;
  viewport_height: number;
  captured_at: string;
  preview_url: string;
  thumbnail_url?: string;
  state?: string;
  status?: string;
  is_current?: boolean;
  warnings?: string[];
  content_warnings?: string[];
}
export interface CaptureState {
  variant: Variant;
  state?: string;
  last_success_at?: string;
  last_attempt_at?: string;
  next_due_at?: string;
  last_error_code?: string;
  current_version_id?: string | null;
  current?: Snapshot;
}
export interface Bookmark extends Entity {
  title: string;
  url: string;
  domain: string;
  folder_id?: string | null;
  folder_path?: string;
  tags: Tag[];
  is_starred: boolean;
  snapshot_enabled: boolean;
  auto_refresh_paused?: boolean;
  refresh_interval_seconds?: number | null;
  notes?: string;
  preview: Snapshot | null;
  capture_status?: { state: string; last_error_code?: string | null };
  capture_states?: CaptureState[];
  created_at?: string;
  source_folder_path?: string;
}
export interface Settings {
  revision: number;
  tree_revision: number;
  default_refresh_seconds: number;
  active_ai_profile_id?: string | null;
}

export interface CaptureNetworkSettings {
  mode: "direct" | "clash" | "manual";
  proxy_url: string;
  detected_proxy_url: string | null;
  active_proxy_url: string | null;
}
export interface Session {
  auth_mode?: "debug" | "public_key";
  password_login_enabled?: boolean;
  passkey_configured?: boolean;
  file_key_configured?: boolean;
  passkey_origin?: string | null;
  passkey_available?: boolean;
  auth_expires_at?: string | null;
  credential_id?: string | null;
  authenticated: boolean;
  csrf_token: string;
  initialization_required?: boolean;
  active_generation?: string;
  library_epoch?: number;
  maintenance_mode?: boolean;
}
export interface PasskeyRecord {
  id: string;
  name: string;
  rp_id: string;
  created_at: string;
  last_used_at: string | null;
  is_current: boolean;
  can_delete: boolean;
}
export interface PasskeyInventory {
  can_add: boolean;
  items: PasskeyRecord[];
}
export interface EnrollmentGrant {
  enrollment_url: string;
  authorization_code: string;
  expires_at: string;
}
export interface NetworkConnection {
  mode: "http" | "lan_https" | "https";
  adapter: string;
  ipv4: string;
  access_url: string;
  certificate_url: string | null;
  device_enrollment: boolean;
}
export interface RuntimeStatus extends Record<string, unknown> {
  connection?: NetworkConnection | null;
}
export interface PasskeyRegistrationResult {
  registered: boolean;
  authenticated: boolean;
  credential_id: string;
  already_completed: boolean;
  registration_mode: "local" | "bootstrap" | "first_passkey" | "add_device" | "reset";
}
export interface Profile extends Entity {
  name: string;
  base_url: string;
  endpoint?: string;
  model: string;
  protocol: string;
  enabled: boolean;
  output_mode?: string;
  key_configured: boolean;
  key_verified: boolean;
  binding_matches: boolean;
  endpoint_error_code?: string;
}
export interface Proposal extends Entity {
  manual?: boolean;
  capture_failed?: boolean;
  bookmark_id: string;
  bookmark_title?: string;
  bookmark_url?: string;
  current_folder_id?: string | null;
  original_folder_paths?: string[];
  bookmark?: Bookmark;
  title?: string;
  url?: string;
  current_folder_path?: string;
  original_folder_path?: string;
  target_folder_id?: string | null;
  target_path?: string[] | string;
  target?: {
      existing_folder_id?: string | null;
    parent_folder_id?: string | null;
    new_segments?: string[];
  };
  reason: string;
  status: string;
  expected_tree_revision?: number;
}
export interface Job {
  id: string;
  state: string;
  kind?: string;
  type?: string;
  stage?: string;
  error_code?: string;
  error_message?: string;
  variant?: Variant;
  target_id?: string;
  website_count?: number;
}
export interface JobGroup {
  phase?: "planning" | "classifying";
  id?: string;
  group_id?: string;
  kind?: string;
  state: string;
  total?: number;
  completed?: number;
  failed?: number;
  skipped?: number;
  requested_count?: number;
  completed_count?: number;
  failed_count?: number;
  counts?: Record<string, number>;
  completed_sites?: number;
  partial_sites?: number;
  succeeded_sites?: number;
  failed_sites?: number;
  jobs?: Job[];
  device_total?: number;
  device_completed?: number;
  website_total?: number;
  website_completed?: number;
  cancelled_sites?: number;
  queued_sites?: number;
  running_sites?: number;
  retrying_sites?: number;
  pending_count?: number;
  created_at?: string;
  source?: string;
}
export interface TaskCatalog {
  groups: JobGroup[];
  current_group?: JobGroup | null;
  completion_revision?: number;
  active_count: number;
  pending_proposals: number;
}
export interface Operation {
  id?: string;
  operation_id: string;
  kind: string;
  state: string;
  stage: string;
  created_at?: string;
  error_code?: string;
  result?: Record<string, unknown>;
  validation_token?: string;
  manifest?: Record<string, unknown>;
  byte_size?: number;
  download_url?: string;
}
export interface ImportBatch {
  execution_plan?: {
    captures: number;
    ai_requests: number;
    can_execute: boolean;
    entry_limit: number;
    confirmation_token: string;
  };
  id: string;
  state: string;
  total: number;
  valid: number;
  duplicate: number;
    invalid: number;
    stage: "records" | "captures" | "classification" | "review";
    progress: number;
    capture_total: number;
    capture_completed: number;
    capture_failed_sites: number;
    classification_total: number;
    classification_completed: number;
    ai_status: string;
    ai_error_code?: string;
    review_ready: boolean;
    review_notified: boolean;
  imported?: number;
  completed?: number;
  entries?: {
    title: string;
    url: string;
    original_folder_path?: string;
    status: string;
    error_code?: string;
  }[];
}
export interface Envelope<T> {
  data: T;
  page?: { number: number; size: number; total: number };
}
