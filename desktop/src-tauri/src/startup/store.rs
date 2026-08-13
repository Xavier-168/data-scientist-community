use std::sync::Arc;

use tokio::sync::{watch, Mutex};

use super::model::StartupSnapshot;

#[derive(Clone)]
pub struct StartupStore {
    sender: watch::Sender<StartupSnapshot>,
    gate: Arc<Mutex<()>>,
}

impl Default for StartupStore {
    fn default() -> Self {
        let (sender, _) = watch::channel(StartupSnapshot::default());
        Self {
            sender,
            gate: Arc::new(Mutex::new(())),
        }
    }
}

impl StartupStore {
    pub fn snapshot(&self) -> StartupSnapshot {
        self.sender.borrow().clone()
    }

    pub async fn update(&self, mutate: impl FnOnce(&mut StartupSnapshot)) -> StartupSnapshot {
        let _guard = self.gate.lock().await;
        let mut next = self.snapshot();
        let previous = next.occurred_at_ms;
        mutate(&mut next);
        let wall_clock = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as u64;
        next.occurred_at_ms = wall_clock.max(previous.saturating_add(1));
        self.sender.send_replace(next.clone());
        next
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn occurred_at_ms_strictly_increases_even_within_one_millisecond() {
        let store = StartupStore::default();
        let first = store.update(|state| state.core.percent = 1).await;
        let second = store.update(|state| state.core.percent = 2).await;
        assert!(second.occurred_at_ms > first.occurred_at_ms);
    }
}
