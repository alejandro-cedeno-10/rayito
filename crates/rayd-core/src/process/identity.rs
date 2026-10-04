//! Who a process runs as. The default is the unprivileged `user` created by
//! the image; root is refused unless the image opted in through `rayd`'s own
//! environment (`RAYITO_ALLOW_ROOT=1`), never through the request.

use super::error::ProcessError;

pub const DEFAULT_USERNAME: &str = "user";
pub const ROOT_USERNAME: &str = "root";
/// Environment variable of `rayd` (image level, never per request) that
/// allows `username == "root"`.
pub const ALLOW_ROOT_ENV: &str = "RAYITO_ALLOW_ROOT";
/// Floor of the image's regular accounts. Below it live the system accounts,
/// which the IMDS blackhole of M6 (`uidrange 1000-65535`) does not cover and
/// which may own files of the root group.
pub const MIN_UNPRIVILEGED_ID: u32 = 1000;
/// Ceiling of the same range: the IMDS blackhole and the egress and DNS
/// rules match `uidrange 1000-65535` (`network::route_plan::
/// SANDBOX_UID_RANGE`), so an account above it would run with neither and
/// is refused like a system account.
pub const MAX_UNPRIVILEGED_ID: u32 = 65_535;
/// The root group: membership grants read access to `/root` and to every
/// root-group file, which is what T11 and T15 assume nobody reaches.
pub const ROOT_GROUP_ID: u32 = 0;
const ROOT_USER_ID: u32 = 0;

/// Resolved from the image's user database before `fork`, so the child only
/// has to `setgroups`/`setgid`/`setuid` with numbers.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcessIdentity {
    pub uid: u32,
    pub gid: u32,
    pub groups: Vec<u32>,
    pub username: String,
    pub home: String,
    /// Login shell from the user database (`pw_shell`), `/bin/sh` when the
    /// entry has none; a PTY spawns it as `<shell> -i -l`.
    pub shell: String,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct UserPolicy {
    pub allow_root: bool,
}

impl UserPolicy {
    /// Only the literal value `1` enables root, so a stray `RAYITO_ALLOW_ROOT=`
    /// or `=true` in an image keeps the safe default.
    #[must_use]
    pub fn from_env_flag(value: Option<&str>) -> Self {
        Self {
            allow_root: value == Some("1"),
        }
    }

    pub fn authorize(self, username: &str) -> Result<(), ProcessError> {
        if username == ROOT_USERNAME && !self.allow_root {
            return Err(ProcessError::RootNotAllowed);
        }
        Ok(())
    }

    /// Second gate after the lookup, and the only one that sees numbers:
    /// `authorize` only ever saw a name, so an alias of uid 0, a system
    /// account below the floor, an account above the ceiling and a member
    /// of the root group all arrive here. `RAYITO_ALLOW_ROOT=1` is an image-level opt-in and still
    /// bypasses the whole gate, exactly as before.
    pub fn authorize_identity(self, identity: &ProcessIdentity) -> Result<(), ProcessError> {
        if self.allow_root {
            return Ok(());
        }
        if identity.uid == ROOT_USER_ID {
            return Err(ProcessError::RootNotAllowed);
        }
        if is_unprivileged(identity) {
            Ok(())
        } else {
            Err(ProcessError::PrivilegedAccount)
        }
    }

    /// The same policy with the image's root opt-in dropped, so a caller that
    /// must refuse root whatever the image allows —persistence never archives
    /// `/root`— reaches the single gate instead of repeating a `uid == 0`
    /// check of its own.
    #[must_use]
    pub fn without_root(self) -> Self {
        Self { allow_root: false }
    }
}

/// Uid and gid inside the range the network rules cover, and never a
/// member of the root group.
fn is_unprivileged(identity: &ProcessIdentity) -> bool {
    let sandbox_range = MIN_UNPRIVILEGED_ID..=MAX_UNPRIVILEGED_ID;
    sandbox_range.contains(&identity.uid)
        && sandbox_range.contains(&identity.gid)
        && !identity.groups.contains(&ROOT_GROUP_ID)
}

/// Request `User.username` if present and non-empty, else the `user` default
/// of the `/run` payload, else `"user"`.
#[must_use]
pub fn resolve_username(request: Option<&str>, defaults: Option<&str>) -> String {
    non_empty(request)
        .or_else(|| non_empty(defaults))
        .unwrap_or(DEFAULT_USERNAME)
        .to_owned()
}

fn non_empty(value: Option<&str>) -> Option<&str> {
    value.filter(|name| !name.is_empty())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn identity(uid: u32) -> ProcessIdentity {
        account(uid, uid, &[uid])
    }

    fn account(uid: u32, gid: u32, groups: &[u32]) -> ProcessIdentity {
        ProcessIdentity {
            uid,
            gid,
            groups: groups.to_vec(),
            username: "someone".to_owned(),
            home: "/home/someone".to_owned(),
            shell: "/bin/bash".to_owned(),
        }
    }

    #[test]
    fn request_wins_over_payload_default_over_user() {
        assert_eq!(resolve_username(Some("alice"), Some("bob")), "alice");
        assert_eq!(resolve_username(None, Some("bob")), "bob");
        assert_eq!(resolve_username(Some(""), Some("bob")), "bob");
        assert_eq!(resolve_username(None, None), "user");
        assert_eq!(resolve_username(Some(""), Some("")), "user");
    }

    #[test]
    fn root_is_refused_unless_allowed() {
        let strict = UserPolicy::default();
        assert_eq!(strict.authorize("root"), Err(ProcessError::RootNotAllowed));
        assert_eq!(strict.authorize("user"), Ok(()));
        assert_eq!(
            strict.authorize_identity(&identity(0)),
            Err(ProcessError::RootNotAllowed)
        );
        assert_eq!(strict.authorize_identity(&identity(1000)), Ok(()));
        let permissive = UserPolicy { allow_root: true };
        assert_eq!(permissive.authorize("root"), Ok(()));
        assert_eq!(permissive.authorize_identity(&identity(0)), Ok(()));
    }

    #[test]
    fn system_accounts_are_refused() {
        let strict = UserPolicy::default();
        let privileged = [
            account(11, 0, &[0]),
            account(1, 1, &[1]),
            account(1000, 1000, &[1000, 0]),
            account(1000, 0, &[1000]),
        ];
        for identity in &privileged {
            assert_eq!(
                strict.authorize_identity(identity),
                Err(ProcessError::PrivilegedAccount),
                "uid {} gid {}",
                identity.uid,
                identity.gid
            );
        }
        assert_eq!(
            strict.authorize_identity(&account(1000, 1000, &[1000])),
            Ok(())
        );
        assert_eq!(
            strict.authorize_identity(&account(0, 0, &[0])),
            Err(ProcessError::RootNotAllowed)
        );
        let permissive = UserPolicy { allow_root: true };
        for identity in &privileged {
            assert_eq!(permissive.authorize_identity(identity), Ok(()));
        }
        assert_eq!(permissive.authorize_identity(&account(0, 0, &[0])), Ok(()));
    }

    #[test]
    fn ids_above_the_sandbox_uid_range_are_refused() {
        let strict = UserPolicy::default();
        let above = MAX_UNPRIVILEGED_ID + 1;
        for (case, identity) in [
            ("uid above", account(above, 1000, &[1000])),
            ("gid above", account(1000, above, &[1000])),
            ("both above", account(above, above, &[above])),
        ] {
            assert_eq!(
                strict.authorize_identity(&identity),
                Err(ProcessError::PrivilegedAccount),
                "{case}"
            );
        }
        assert_eq!(
            strict.authorize_identity(&identity(MAX_UNPRIVILEGED_ID)),
            Ok(())
        );
        assert_eq!(
            UserPolicy { allow_root: true }.authorize_identity(&identity(above)),
            Ok(())
        );
    }

    #[test]
    fn without_root_drops_the_image_opt_in() {
        let permissive = UserPolicy { allow_root: true };
        assert_eq!(
            permissive.without_root().authorize_identity(&identity(0)),
            Err(ProcessError::RootNotAllowed)
        );
        assert_eq!(
            permissive.without_root().authorize("root"),
            Err(ProcessError::RootNotAllowed)
        );
        assert_eq!(UserPolicy::default().without_root(), UserPolicy::default());
    }

    #[test]
    fn only_the_literal_one_enables_root() {
        assert!(UserPolicy::from_env_flag(Some("1")).allow_root);
        assert!(!UserPolicy::from_env_flag(Some("true")).allow_root);
        assert!(!UserPolicy::from_env_flag(Some("")).allow_root);
        assert!(!UserPolicy::from_env_flag(None).allow_root);
    }
}
