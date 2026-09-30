//! Why a policy was refused or could not be enforced. Messages are shown to
//! the SDK user through the gRPC status, so they are in Spanish and never
//! quote an entry, an address or a credential: only the list name, the
//! index and fixed step names.

use std::fmt;

use thiserror::Error;

use super::{
    EGRESS_MAX_ENTRIES_PER_LIST, EGRESS_MAX_HOSTNAME_ENTRIES, EGRESS_MAX_ROUTES_PER_FAMILY,
    EGRESS_PROXY_CREDENTIAL_MAX_BYTES,
};
use crate::wire_tokens::{EGRESS_UPDATE_FAILED, EGRESS_VERIFY_FAILED};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EgressList {
    AllowOut,
    DenyOut,
}

impl EgressList {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::AllowOut => "allow_out",
            Self::DenyOut => "deny_out",
        }
    }
}

impl fmt::Display for EgressList {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum NetworkError {
    #[error("{list}[{index}]: no es un CIDR, una IP ni un nombre de host válido")]
    InvalidEntry { list: EgressList, index: usize },
    #[error("deny_out[{index}]: los nombres de host no se admiten en deny_out")]
    HostnameInDenyOut { index: usize },
    #[error("{list}: admite como máximo {EGRESS_MAX_ENTRIES_PER_LIST} entradas")]
    TooManyEntries { list: EgressList },
    #[error("allow_out: admite como máximo {EGRESS_MAX_HOSTNAME_ENTRIES} nombres de host")]
    TooManyHostnames,
    #[error(
        "la política genera más de {EGRESS_MAX_ROUTES_PER_FAMILY} rutas por familia de direcciones"
    )]
    PolicyTooComplex,
    #[error("egress_proxy: la dirección debe tener la forma host:puerto o [IPv6]:puerto")]
    InvalidProxyAddress,
    #[error(
        "egress_proxy: usuario y contraseña deben tener entre 1 y {EGRESS_PROXY_CREDENTIAL_MAX_BYTES} bytes, y la contraseña exige usuario"
    )]
    InvalidProxyCredentials,
    #[error(
        "egress_proxy: la dirección apunta a un destino prohibido (loopback, IMDS, multicast o no especificada)"
    )]
    ProxyForbiddenAddress,
    #[error("egress_proxy: el nombre del proxy no resuelve")]
    ProxyUnresolvable,
    #[error("la imagen no tiene CAP_NET_ADMIN: la política de egress exige rayito-base-caps")]
    NoNetAdmin,
    #[error("{EGRESS_UPDATE_FAILED}: {step}")]
    InstallFailed { step: &'static str },
    #[error("{EGRESS_VERIFY_FAILED}")]
    VerifyFailed,
}

/// Which gRPC status class an error maps to (design D11): the caller sent
/// something malformed (`INVALID_ARGUMENT`), the image cannot enforce it
/// (`FAILED_PRECONDITION`) or the guest failed to (`INTERNAL`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NetworkStatusClass {
    InvalidArgument,
    FailedPrecondition,
    Internal,
}

impl NetworkError {
    /// Exhaustive on purpose: a new variant must pick its class here.
    #[must_use]
    pub fn status_class(&self) -> NetworkStatusClass {
        match self {
            Self::InvalidEntry { .. }
            | Self::HostnameInDenyOut { .. }
            | Self::TooManyEntries { .. }
            | Self::TooManyHostnames
            | Self::PolicyTooComplex
            | Self::InvalidProxyAddress
            | Self::InvalidProxyCredentials
            | Self::ProxyForbiddenAddress
            | Self::ProxyUnresolvable => NetworkStatusClass::InvalidArgument,
            Self::NoNetAdmin => NetworkStatusClass::FailedPrecondition,
            Self::InstallFailed { .. } | Self::VerifyFailed => NetworkStatusClass::Internal,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_variant_has_its_status_class() {
        for (error, class) in [
            (
                NetworkError::InvalidEntry {
                    list: EgressList::AllowOut,
                    index: 0,
                },
                NetworkStatusClass::InvalidArgument,
            ),
            (
                NetworkError::HostnameInDenyOut { index: 0 },
                NetworkStatusClass::InvalidArgument,
            ),
            (
                NetworkError::TooManyEntries {
                    list: EgressList::DenyOut,
                },
                NetworkStatusClass::InvalidArgument,
            ),
            (
                NetworkError::TooManyHostnames,
                NetworkStatusClass::InvalidArgument,
            ),
            (
                NetworkError::PolicyTooComplex,
                NetworkStatusClass::InvalidArgument,
            ),
            (
                NetworkError::InvalidProxyAddress,
                NetworkStatusClass::InvalidArgument,
            ),
            (
                NetworkError::InvalidProxyCredentials,
                NetworkStatusClass::InvalidArgument,
            ),
            (
                NetworkError::ProxyForbiddenAddress,
                NetworkStatusClass::InvalidArgument,
            ),
            (
                NetworkError::ProxyUnresolvable,
                NetworkStatusClass::InvalidArgument,
            ),
            (
                NetworkError::NoNetAdmin,
                NetworkStatusClass::FailedPrecondition,
            ),
            (
                NetworkError::InstallFailed { step: "add_rule" },
                NetworkStatusClass::Internal,
            ),
            (NetworkError::VerifyFailed, NetworkStatusClass::Internal),
        ] {
            assert_eq!(error.status_class(), class, "{error:?}");
        }
    }
}
