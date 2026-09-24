//! The read-after-upload barrier (design D11): before a `Read`, `Stat` or
//! `ListDir` that touches an armed ticket's destination, and before every
//! new process, cell or terminal, the RPC asks each concerned `Waiting`
//! ticket to poll now and waits up to one 2 s budget for those probes; a
//! found object, or a ticket already moving bytes, is waited for until its
//! import ends (bounded only by the RPC's own deadline and cancellation).
//! It fails open: a probe that finds nothing or does not answer in time
//! lets the RPC go on, and a failed import does not fail the RPC. With no
//! armed ticket it is one atomic load.

use std::sync::Arc;
use std::time::Instant;

use rayd_core::transfer::{BarrierOutcome, BarrierQuery, BarrierTicket, TransferPhase, select};
use tokio::sync::watch;
use tokio::time::Instant as TokioInstant;

use super::import::millis;
use super::manager::TransferHub;
use crate::filesystem::FilesystemManager;

/// A cheap handle every gRPC service holds; `disabled()` where no transfer
/// manager is wired.
#[derive(Clone, Default)]
pub struct TransferBarrier {
    hub: Option<Arc<TransferHub>>,
}

/// What one ticket turned out to need.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum TicketOutcome {
    Finished,
    Probed,
    BudgetExhausted,
    Waited,
}

impl TransferBarrier {
    pub(crate) fn new(hub: Arc<TransferHub>) -> Self {
        Self { hub: Some(hub) }
    }

    #[must_use]
    pub fn disabled() -> Self {
        Self::default()
    }

    /// `Process.Start`, `Code.Execute`, `Pty.Create`: every armed ticket.
    pub async fn before_workload(&self, rpc: &'static str) {
        let Some(hub) = self.armed_hub() else {
            return;
        };
        run(hub, rpc, &BarrierQuery::Workload).await;
    }

    /// `Read`/`Stat` (`tree = false`) and `ListDir` (`tree = true`): the
    /// tickets whose destination is the path, or lies under it.
    pub async fn before_path(
        &self,
        rpc: &'static str,
        files: &FilesystemManager,
        user: Option<&str>,
        path: &str,
        tree: bool,
    ) {
        let Some(hub) = self.armed_hub() else {
            return;
        };
        let Some((request_path, canonical)) = files.barrier_target(user, path).await else {
            return;
        };
        let query = if tree {
            BarrierQuery::Tree {
                canonical_root: &canonical,
            }
        } else {
            BarrierQuery::Path {
                request_path: &request_path,
                canonical: &canonical,
            }
        };
        run(hub, rpc, &query).await;
    }

    fn armed_hub(&self) -> Option<&TransferHub> {
        self.hub.as_deref().filter(|hub| hub.armed_count() > 0)
    }
}

async fn run(hub: &TransferHub, rpc: &'static str, query: &BarrierQuery<'_>) {
    let started = Instant::now();
    let tickets = hub.armed_tickets();
    let selected: Vec<&BarrierTicket> = select(&tickets, query);
    let deadline = TokioInstant::now() + hub.settings.probe_budget;
    let mut followed = Vec::with_capacity(selected.len());
    for ticket in &selected {
        let Some(handle) = hub.handle(&ticket.id) else {
            continue;
        };
        let receiver = handle.snapshots.subscribe();
        let probes_before = receiver.borrow().probes;
        if ticket.phase == TransferPhase::Waiting {
            handle.poll_now.notify_one();
        }
        followed.push((receiver, probes_before));
    }
    let mut outcome = None;
    for (receiver, probes_before) in followed {
        let ticket = follow(receiver, probes_before, deadline).await;
        outcome = outcome.max(Some(ticket));
    }
    let outcome = match outcome {
        None | Some(TicketOutcome::Finished) => BarrierOutcome::None,
        Some(TicketOutcome::Probed) => BarrierOutcome::Probed,
        Some(TicketOutcome::BudgetExhausted) => BarrierOutcome::BudgetExhausted,
        Some(TicketOutcome::Waited) => BarrierOutcome::Waited,
    };
    tracing::info!(
        rpc,
        tickets = selected.len(),
        waited_ms = millis(started.elapsed()),
        outcome = outcome.as_str(),
        "barrier"
    );
}

/// A running import, or a waiting one whose object was seen, is followed to
/// its end; a waiting one is given until the budget for one more probe.
async fn follow(
    mut receiver: watch::Receiver<rayd_core::transfer::TransferSnapshot>,
    probes_before: u32,
    deadline: TokioInstant,
) -> TicketOutcome {
    let current = receiver.borrow_and_update().clone();
    if current.phase.is_terminal() {
        return TicketOutcome::Finished;
    }
    if current.phase == TransferPhase::Running || current.object_seen {
        return wait_terminal(&mut receiver).await;
    }
    let probed = tokio::time::timeout_at(deadline, async {
        receiver
            .wait_for(|snapshot| {
                snapshot.probes > probes_before || snapshot.phase != TransferPhase::Waiting
            })
            .await
            .map(|snapshot| (snapshot.phase, snapshot.object_seen))
    })
    .await;
    match probed {
        Err(_) => TicketOutcome::BudgetExhausted,
        Ok(Err(_)) => TicketOutcome::Finished,
        Ok(Ok((phase, seen))) => {
            if phase.is_terminal() {
                TicketOutcome::Finished
            } else if phase == TransferPhase::Running || seen {
                wait_terminal(&mut receiver).await
            } else {
                TicketOutcome::Probed
            }
        }
    }
}

async fn wait_terminal(
    receiver: &mut watch::Receiver<rayd_core::transfer::TransferSnapshot>,
) -> TicketOutcome {
    let _ = receiver
        .wait_for(|snapshot| snapshot.phase.is_terminal())
        .await;
    TicketOutcome::Waited
}
