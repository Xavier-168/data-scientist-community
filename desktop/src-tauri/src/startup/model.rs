use serde::{Deserialize, Serialize};
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum StartupPhase {
    WindowReady,
    CoreChecking,
    CorePreparing,
    ApiStarting,
    ApiReady,
    CollectorPreparing,
    Ready,
    Degraded,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum LaneStatus {
    Idle,
    Checking,
    Preparing,
    Ready,
    Failed,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct StartupLane {
    pub status: LaneStatus,
    pub percent: u8,
    pub message: String,
}

impl Default for StartupLane {
    fn default() -> Self {
        Self {
            status: LaneStatus::Idle,
            percent: 0,
            message: String::new(),
        }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RetryStage {
    Core,
    Collector,
    Install,
    Sidecar,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RecoverableError {
    pub stage: RetryStage,
    pub code: String,
    pub message: String,
    pub retryable: bool,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct StartupSnapshot {
    pub phase: StartupPhase,
    pub core: StartupLane,
    pub collector: StartupLane,
    pub install: StartupLane,
    pub api_ready: bool,
    pub can_collect: bool,
    pub recoverable_error: Option<RecoverableError>,
    pub occurred_at_ms: u64,
}

impl Default for StartupSnapshot {
    fn default() -> Self {
        Self {
            phase: StartupPhase::WindowReady,
            core: StartupLane::default(),
            collector: StartupLane::default(),
            install: StartupLane::default(),
            api_ready: false,
            can_collect: false,
            recoverable_error: None,
            occurred_at_ms: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_millis() as u64,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn snapshot_is_camel_case_and_percent_is_integer_0_to_100() {
        let snapshot = StartupSnapshot::default();
        let value = serde_json::to_value(snapshot).unwrap();
        assert_eq!(value["phase"], "window_ready");
        assert_eq!(value["core"]["percent"], 0);
        assert_eq!(value["apiReady"], false);
        assert_eq!(value["canCollect"], false);
        assert!(value.get("api_ready").is_none());
    }

    #[test]
    fn percent_rejects_values_outside_u8_wire_range() {
        let mut value = serde_json::to_value(StartupSnapshot::default()).unwrap();
        value["core"]["percent"] = serde_json::json!(256);
        assert!(serde_json::from_value::<StartupSnapshot>(value).is_err());
    }
}
