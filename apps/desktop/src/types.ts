export type ServiceStatus = {
  name: string;
  state: string;
  error?: string | null;
  last_success?: string | null;
};

export type DesktopConfig = {
  wechat: { data_dir: string; config_file: string };
  ai: {
    enabled: boolean;
    base_url: string;
    api_key_set: boolean;
    batch_model: string;
    vision_model: string;
  };
  sources: {
    instagram: { enabled: boolean; username: string };
    linkedin: { enabled: boolean };
    faces: { enabled: boolean };
  };
  autostart: boolean;
};

export type DesktopStatus = {
  state: string;
  operation?: string;
  configured?: boolean;
  account_created?: boolean;
  web_url?: string | null;
  services: ServiceStatus[];
  sources: ServiceStatus[];
  config: DesktopConfig;
  error?: string | null;
  logs_dir?: string;
  wechat_candidates?: string[];
  profiles?: number;
  posts_imported?: number;
  messages_imported?: number;
  pending_jobs?: number;
  import_phases?: ImportPhase[];
  pipeline?: {
    consumers: { provider: string; state: string; heartbeat_age_seconds: number; retained_wal_bytes: number }[];
    projection: { count: number; oldest_seconds: number };
    media_waiting: { reason: string; count: number }[];
    lanes: { lane: string; status: string; count: number; oldest_seconds: number; retry_waiting: number }[];
  };
};

export type ImportPhase = {
  source: string;
  phase: string;
  state: string;
  stage?: string | null;
  last_success?: string | null;
  progress: Record<string, number>;
};

export const defaultConfig: DesktopConfig = {
  wechat: { data_dir: "", config_file: "" },
  ai: {
    enabled: false,
    base_url: "",
    api_key_set: false,
    batch_model: "",
    vision_model: "",
  },
  sources: {
    instagram: { enabled: false, username: "" },
    linkedin: { enabled: false },
    faces: { enabled: false },
  },
  autostart: false,
};

export function normalizeConfig(
  config?: Partial<DesktopConfig>,
): DesktopConfig {
  return {
    ...defaultConfig,
    ...config,
    wechat: { ...defaultConfig.wechat, ...config?.wechat },
    ai: { ...defaultConfig.ai, ...config?.ai },
    sources: {
      instagram: {
        ...defaultConfig.sources.instagram,
        ...config?.sources?.instagram,
      },
      linkedin: {
        ...defaultConfig.sources.linkedin,
        ...config?.sources?.linkedin,
      },
      faces: { ...defaultConfig.sources.faces, ...config?.sources?.faces },
    },
  };
}
