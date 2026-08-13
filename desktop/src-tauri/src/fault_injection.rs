#[derive(Clone, Debug, Default)]
pub struct FaultInjection {
    pub core_hash: bool,
    pub collector_hash: bool,
    pub disk_full: bool,
    pub collector_delay_ms: u64,
    pub install: bool,
    pub state_root: Option<std::path::PathBuf>,
    #[cfg(any(test, feature = "test-harness"))]
    pub collector_gate_entered: Option<std::sync::Arc<tokio::sync::Notify>>,
    #[cfg(any(test, feature = "test-harness"))]
    pub collector_gate_release: Option<std::sync::Arc<tokio::sync::Notify>>,
}

#[cfg(debug_assertions)]
fn from_lookup(mut lookup: impl FnMut(&str) -> Option<String>) -> FaultInjection {
    if lookup("YIRENGONGIS_TEST_MODE").as_deref() != Some("1") {
        return FaultInjection::default();
    }
    FaultInjection {
        core_hash: lookup("YIRENGONGIS_FAULT_CORE_HASH").as_deref() == Some("1"),
        collector_hash: lookup("YIRENGONGIS_FAULT_COLLECTOR_HASH").as_deref() == Some("1"),
        disk_full: lookup("YIRENGONGIS_FAULT_DISK_FULL").as_deref() == Some("1"),
        collector_delay_ms: lookup("YIRENGONGIS_FAULT_COLLECTOR_DELAY_MS")
            .and_then(|value| value.parse().ok())
            .unwrap_or(0),
        install: lookup("YIRENGONGIS_FAULT_INSTALL").as_deref() == Some("1"),
        state_root: lookup("YIRENGONGIS_STATE_ROOT").map(std::path::PathBuf::from),
        ..FaultInjection::default()
    }
}

#[cfg(debug_assertions)]
pub fn from_env() -> FaultInjection {
    let mut faults = from_lookup(|key| std::env::var(key).ok());
    if faults.state_root.is_some() {
        faults.state_root =
            std::env::var_os("YIRENGONGIS_STATE_ROOT").map(std::path::PathBuf::from);
    }
    faults
}

#[cfg(not(debug_assertions))]
pub fn from_env() -> FaultInjection {
    FaultInjection::default()
}

#[cfg(not(debug_assertions))]
const _: () = assert!(!cfg!(debug_assertions));

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use super::*;

    #[test]
    fn parsed_faults_require_explicit_test_mode_without_mutating_process_environment() {
        let disabled = HashMap::from([("YIRENGONGIS_FAULT_INSTALL", "1")]);
        assert!(!from_lookup(|key| disabled.get(key).map(ToString::to_string)).install);

        let enabled = HashMap::from([
            ("YIRENGONGIS_TEST_MODE", "1"),
            ("YIRENGONGIS_FAULT_INSTALL", "1"),
            ("YIRENGONGIS_FAULT_COLLECTOR_DELAY_MS", "250"),
            ("YIRENGONGIS_STATE_ROOT", "/tmp/test-state"),
        ]);
        let faults = from_lookup(|key| enabled.get(key).map(ToString::to_string));
        assert!(faults.install);
        assert_eq!(faults.collector_delay_ms, 250);
        assert_eq!(
            faults.state_root,
            Some(std::path::PathBuf::from("/tmp/test-state"))
        );
    }
}
