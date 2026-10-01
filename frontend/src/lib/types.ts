// Shapes of the daemon's REST and WebSocket data used by the UI.

export type JsonEvent = { type: string; [key: string]: any };

export interface Source {
  url: string;
  title: string;
}

export type MessageKind = "user" | "assistant" | "progress" | "system" | "error";

export interface ChatMessage {
  id: number;
  kind: MessageKind;
  text: string;
  label?: string; // e.g. "🎙 said" / "🔊 JARVIS"
  sources?: Source[];
}

export interface PlanStep {
  title: string;
  status?: "pending" | "in_progress" | "done" | "failed" | "skipped";
}

export interface ActivityItem {
  id: number;
  text: string;
  failed?: boolean;
}

export interface SetupStatus {
  provider: string;
  model: string;
  api_key_set: boolean;
  llm_ready: boolean;
  voice_models_ready: boolean;
  voice_enabled: boolean;
  voice_state: string;
  screen_control: string;
  allowed_roots: string[];
  onboarded: boolean;
  spend_today_usd: number;
  daily_cap_usd: number;
  documents: DocumentsStatus;
  connected_accounts: number;
  overlay?: { enabled: boolean; available: boolean; running: boolean; interact_hotkey: string; hide_hotkey: string };
}

export interface Status {
  version: string;
  llm_provider: string;
  busy: boolean;
  setup: SetupStatus;
}

export interface DocumentsStatus {
  enabled: boolean;
  state?: string;
  done?: number;
  total?: number;
  files?: number;
  failed?: number;
  error?: string;
  model_ready?: boolean;
}

export interface MapsFeature {
  enabled: boolean;
  sky: boolean;
  network: boolean;
  situation: boolean;
  sky_radius_km: number;
}

export interface SettingsView {
  profile: {
    name: string;
    address: string;
    nickname: string;
    location: string;
    latitude: number | null;
    longitude: number | null;
    onboarded: boolean;
  };
  persona: { style: string; pushback: boolean; progress_updates: boolean; quick_replies: boolean };
  voice: { tts_voice: string; tts_speed: number; activation: string };
  features: {
    web_research: boolean;
    documents: { enabled: boolean; folders: string[] };
    email: { enabled: boolean; provider: string; account: string };
    protocols: { enabled: boolean; briefing_time: string | null };
    maps: MapsFeature;
    hud: { enabled: boolean; dashboard: boolean };
    phone: { enabled: boolean; platform: string };
    smart_home: { enabled: boolean; url: string };
    webcam: boolean;
    helpers: boolean;
  };
  budget: { daily_usd: number; background: string };
  connections: {
    microsoft_client_id: string;
    microsoft_tenant: string;
    google_client_id: string;
    google_client_secret: string;
  };
  voices: string[];
}

export interface Task {
  id: string;
  goal: string;
  status: string;
  started_at: string;
  resumable: boolean;
}

export interface Memory {
  id: number;
  content: string;
  kind: string;
}

export interface Workflow {
  name: string;
  description: string;
  parameters: string[];
  run_count: number;
}

export interface Account {
  id: string;
  provider: string;
  email: string;
  capabilities: string[];
}

export interface ConnectionsStatus {
  accounts: Account[];
  apps: { microsoft: boolean; google: boolean };
  capabilities: Record<string, string>;
  provider_capabilities: Record<string, string[]>;
  signing_in?: boolean;
}

export interface ApprovalRequest {
  id: string;
  tool: string;
  summary: string;
  risk: string;
  details?: string;
}

export interface AskRequest {
  id: string;
  question: string;
}

export interface MapFocus {
  view: "sky" | "network" | "situation";
  lat?: number;
  lon?: number;
  label?: string;
  at: number;
}

export interface HomePlace {
  name: string;
  lat: number;
  lon: number;
}

export interface Aircraft {
  icao24: string;
  callsign: string;
  country: string;
  lat: number;
  lon: number;
  altitude_m: number | null;
  speed_kmh: number | null;
  heading: number | null;
  vertical_ms: number | null;
  on_ground: boolean;
  squawk: string;
  distance_km: number;
  alert: string;
}

export interface SkyView {
  home: HomePlace | null;
  radius_km: number;
  aircraft?: Aircraft[];
  fetched_at?: number;
  error?: string;
  source?: string;
}

export interface Peer {
  ip: string;
  ports: number[];
  process: string;
  pid: number | null;
  connections: number;
  country: { code: string; name: string; lat: number; lon: number } | null;
  flags: string[];
}

export interface NetworkView {
  home: HomePlace | null;
  taken_at: number;
  peers: Peer[];
  listening: { port: number; address: string; process: string; pid: number | null }[];
  geo_ready: boolean;
  geo_downloading: boolean;
  geo_error: string;
  source: string;
}

export interface Weather {
  place: string;
  time: string;
  temperature_c: number;
  feels_like_c: number;
  humidity_pct: number;
  wind_kmh: number;
  wind_dir_deg: number;
  precipitation_mm: number;
  conditions: string;
  is_day: boolean;
  hours: { time: string; temp_c: number; rain_pct: number; conditions: string }[];
  days: {
    date: string;
    conditions: string;
    max_c: number;
    min_c: number;
    rain_pct: number;
    sunrise: string;
    sunset: string;
  }[];
}

export interface AgendaItem {
  subject: string;
  start: string;
  end: string;
  location: string;
  account: string;
  lat?: number;
  lon?: number;
  place?: string;
}

export interface SituationView {
  home: HomePlace | null;
  weather?: Weather;
  weather_error?: string;
  events: AgendaItem[];
  source: string;
}
