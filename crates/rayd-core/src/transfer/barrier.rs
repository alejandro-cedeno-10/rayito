//! Which armed upload tickets an RPC must wait for (design D11), so
//! E2B's "PUT, then read or run" is deterministic without `wait()`: a
//! `Read` or `Stat` of a ticket's destination, a `ListDir` whose root
//! contains it, and every new process, cell or terminal for all of them.
//! Pure selection; the probe-and-wait mechanics live in the adapter.

use super::registry::{TransferId, TransferPhase};

/// What the RPC touches. Paths are canonical (symlinks resolved, the
/// would-be path of a file that does not exist yet) except
/// `request_path`, which is the normalised request path.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BarrierQuery<'a> {
    /// `Read` and `Stat`.
    Path {
        request_path: &'a str,
        canonical: &'a str,
    },
    /// `ListDir`.
    Tree { canonical_root: &'a str },
    /// `Process.Start`, `Code.Execute`, `Pty.Create`.
    Workload,
}

/// An armed ticket as the barrier sees it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BarrierTicket {
    pub id: TransferId,
    pub phase: TransferPhase,
    pub request_path: String,
    pub destination: String,
}

/// What a barrier run did, for its log line.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BarrierOutcome {
    /// No ticket concerned the RPC.
    None,
    /// Tickets were probed and none had its object yet.
    Probed,
    /// At least one import was waited for until it finished.
    Waited,
    /// The probe budget ran out before every probe answered.
    BudgetExhausted,
}

impl BarrierOutcome {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::None => "none",
            Self::Probed => "probed",
            Self::Waited => "waited",
            Self::BudgetExhausted => "budget_exhausted",
        }
    }
}

#[must_use]
pub fn select<'t>(
    tickets: &'t [BarrierTicket],
    query: &BarrierQuery<'_>,
) -> Vec<&'t BarrierTicket> {
    tickets
        .iter()
        .filter(|ticket| concerns(ticket, query))
        .collect()
}

fn concerns(ticket: &BarrierTicket, query: &BarrierQuery<'_>) -> bool {
    match *query {
        BarrierQuery::Path {
            request_path,
            canonical,
        } => ticket.destination == canonical || ticket.request_path == request_path,
        BarrierQuery::Tree { canonical_root } => {
            is_component_ancestor(canonical_root, &ticket.destination)
        }
        BarrierQuery::Workload => true,
    }
}

/// `root` is `path` itself or one of its directories, component by
/// component: `/home/user` contains `/home/user/a` but not
/// `/home/username`.
#[must_use]
pub fn is_component_ancestor(root: &str, path: &str) -> bool {
    let root = root.trim_end_matches('/');
    if root.is_empty() {
        return path.starts_with('/');
    }
    path == root
        || path
            .strip_prefix(root)
            .is_some_and(|rest| rest.starts_with('/'))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ticket(n: u8, request_path: &str, destination: &str) -> BarrierTicket {
        BarrierTicket {
            id: TransferId::parse(&format!("{n:032x}")).unwrap(),
            phase: TransferPhase::Waiting,
            request_path: request_path.to_owned(),
            destination: destination.to_owned(),
        }
    }

    fn ids(selected: &[&BarrierTicket]) -> Vec<String> {
        selected
            .iter()
            .map(|ticket| ticket.id.as_str().trim_start_matches('0').to_owned())
            .collect()
    }

    fn tickets() -> Vec<BarrierTicket> {
        vec![
            ticket(1, "/home/user/data/in.csv", "/home/user/data/in.csv"),
            ticket(2, "/home/user/link/out.bin", "/srv/real/out.bin"),
            ticket(3, "/tmp/x", "/tmp/x"),
        ]
    }

    #[test]
    fn reads_and_stats_wait_for_their_own_path_only() {
        let tickets = tickets();
        let by_canonical = BarrierQuery::Path {
            request_path: "/home/user/other",
            canonical: "/srv/real/out.bin",
        };
        assert_eq!(ids(&select(&tickets, &by_canonical)), vec!["2"]);
        let by_request = BarrierQuery::Path {
            request_path: "/home/user/data/in.csv",
            canonical: "/somewhere/else",
        };
        assert_eq!(ids(&select(&tickets, &by_request)), vec!["1"]);
        let unrelated = BarrierQuery::Path {
            request_path: "/home/user/data/in.csv.bak",
            canonical: "/home/user/data/in.csv.bak",
        };
        assert!(select(&tickets, &unrelated).is_empty());
    }

    #[test]
    fn listings_wait_for_every_ticket_below_their_root() {
        let tickets = tickets();
        let home = BarrierQuery::Tree {
            canonical_root: "/home/user",
        };
        assert_eq!(ids(&select(&tickets, &home)), vec!["1"]);
        let root = BarrierQuery::Tree {
            canonical_root: "/",
        };
        assert_eq!(ids(&select(&tickets, &root)), vec!["1", "2", "3"]);
        let sibling = BarrierQuery::Tree {
            canonical_root: "/home/user/dat",
        };
        assert!(select(&tickets, &sibling).is_empty());
    }

    #[test]
    fn new_workloads_wait_for_every_armed_ticket() {
        let tickets = tickets();
        assert_eq!(select(&tickets, &BarrierQuery::Workload).len(), 3);
        assert!(select(&[], &BarrierQuery::Workload).is_empty());
    }

    #[test]
    fn ancestry_is_component_wise() {
        assert!(is_component_ancestor("/home/user", "/home/user/a"));
        assert!(is_component_ancestor("/home/user/", "/home/user/a/b"));
        assert!(is_component_ancestor("/home/user", "/home/user"));
        assert!(!is_component_ancestor("/home/user", "/home/username"));
        assert!(!is_component_ancestor("/home/user/a", "/home/user"));
        assert!(is_component_ancestor("/", "/etc"));
        assert_eq!(BarrierOutcome::BudgetExhausted.as_str(), "budget_exhausted");
    }
}
