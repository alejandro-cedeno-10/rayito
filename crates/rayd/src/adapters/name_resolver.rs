//! Owner and group names for `EntryInfo` from the image's user database;
//! off Unix every id stays numeric.

#[cfg(unix)]
pub use unix::NixNameResolver;
#[cfg(unix)]
pub type PlatformNameResolver = unix::NixNameResolver;

#[cfg(not(unix))]
pub type PlatformNameResolver = NumericNameResolver;

use rayd_core::filesystem::NameResolver;

/// Never resolves: the domain falls back to the numeric id as a string.
#[derive(Debug, Default, Clone, Copy)]
pub struct NumericNameResolver;

impl NameResolver for NumericNameResolver {
    fn user_name(&self, _uid: u32) -> Option<String> {
        None
    }

    fn group_name(&self, _gid: u32) -> Option<String> {
        None
    }
}

#[cfg(unix)]
mod unix {
    use nix::unistd::{Gid, Group, Uid, User};
    use rayd_core::filesystem::NameResolver;

    #[derive(Debug, Default, Clone, Copy)]
    pub struct NixNameResolver;

    impl NameResolver for NixNameResolver {
        fn user_name(&self, uid: u32) -> Option<String> {
            User::from_uid(Uid::from_raw(uid))
                .ok()
                .flatten()
                .map(|user| user.name)
        }

        fn group_name(&self, gid: u32) -> Option<String> {
            Group::from_gid(Gid::from_raw(gid))
                .ok()
                .flatten()
                .map(|group| group.name)
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn root_resolves_and_an_absurd_id_does_not() {
            assert_eq!(NixNameResolver.user_name(0).as_deref(), Some("root"));
            assert_eq!(NixNameResolver.user_name(4_000_000_000), None);
            assert_eq!(NixNameResolver.group_name(4_000_000_000), None);
        }
    }
}
