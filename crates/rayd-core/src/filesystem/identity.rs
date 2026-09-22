//! Whose rights a filesystem request runs with: the same username rules as
//! processes (request, then the `/run` payload default, then `user`; root
//! only with the image's opt-in), reduced to what `setfsuid`/`setfsgid`
//! and relative paths need.

use super::error::FilesystemError;
use crate::process::identity::resolve_username;
use crate::process::{LookupError, ProcessError, ProcessIdentity, UserLookup, UserPolicy};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FsIdentity {
    pub uid: u32,
    pub gid: u32,
    pub home: String,
}

impl From<&ProcessIdentity> for FsIdentity {
    fn from(identity: &ProcessIdentity) -> Self {
        Self {
            uid: identity.uid,
            gid: identity.gid,
            home: identity.home.clone(),
        }
    }
}

pub fn resolve_identity(
    request_user: Option<&str>,
    default_user: Option<&str>,
    policy: UserPolicy,
    lookup: &dyn UserLookup,
) -> Result<FsIdentity, FilesystemError> {
    let username = resolve_username(request_user, default_user);
    policy.authorize(&username).map_err(policy_error)?;
    let identity = lookup.lookup(&username).map_err(lookup_error)?;
    policy.authorize_identity(&identity).map_err(policy_error)?;
    Ok(FsIdentity::from(&identity))
}

fn policy_error(error: ProcessError) -> FilesystemError {
    match error {
        ProcessError::RootNotAllowed => FilesystemError::RootNotAllowed,
        ProcessError::PrivilegedAccount => FilesystemError::PrivilegedAccount,
        other => FilesystemError::UserLookupFailed(other.to_string()),
    }
}

fn lookup_error(error: LookupError) -> FilesystemError {
    match error {
        LookupError::UnknownUser => FilesystemError::UnknownUser,
        LookupError::Failed(reason) => FilesystemError::UserLookupFailed(reason),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct FakeUserLookup;

    impl UserLookup for FakeUserLookup {
        fn lookup(&self, username: &str) -> Result<ProcessIdentity, LookupError> {
            let (uid, gid, home) = match username {
                "user" => (1000, 1000, "/home/user"),
                "root" | "toor" => (0, 0, "/root"),
                "operator" => (11, 0, "/root"),
                _ => return Err(LookupError::UnknownUser),
            };
            Ok(ProcessIdentity {
                uid,
                gid,
                groups: vec![gid],
                username: username.to_owned(),
                home: home.to_owned(),
                shell: "/bin/bash".to_owned(),
            })
        }
    }

    fn resolve(
        request: Option<&str>,
        default: Option<&str>,
    ) -> Result<FsIdentity, FilesystemError> {
        resolve_identity(request, default, UserPolicy::default(), &FakeUserLookup)
    }

    #[test]
    fn request_then_default_then_user() {
        assert_eq!(resolve(None, None).unwrap().home, "/home/user");
        assert_eq!(resolve(Some(""), Some("user")).unwrap().uid, 1000);
        assert_eq!(
            resolve(Some("nobody"), None),
            Err(FilesystemError::UnknownUser)
        );
    }

    #[test]
    fn a_system_account_is_refused_although_it_is_not_root() {
        assert_eq!(
            resolve(Some("operator"), None),
            Err(FilesystemError::PrivilegedAccount)
        );
    }

    #[test]
    fn root_is_refused_by_name_and_by_uid_unless_allowed() {
        assert_eq!(
            resolve(Some("root"), None),
            Err(FilesystemError::RootNotAllowed)
        );
        assert_eq!(
            resolve(Some("toor"), None),
            Err(FilesystemError::RootNotAllowed)
        );
        let permissive = UserPolicy { allow_root: true };
        let root = resolve_identity(Some("root"), None, permissive, &FakeUserLookup).unwrap();
        assert_eq!(
            root,
            FsIdentity {
                uid: 0,
                gid: 0,
                home: "/root".to_owned()
            }
        );
    }
}
