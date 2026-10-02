//! Declared exceptions to the guest egress policy that a 0.6 feature opens
//! for traffic leaving as root rather than through the uid-1000 routes
//! ADR-012 governs. Mirrors `RootEgressClass` of
//! `proto/rayito/v1/features.proto`: `Health.features.root_egress` reports
//! only the *class* of a root-egress path that is active, never a host, IP
//! or credential, so the flag is not secret. Foundations defines the
//! enum; each feature that opens a root-egress path (s3-mounts' FUSE
//! daemon credentials, rayd-otlp's `CloudWatch` export, secret-gateway's
//! fixed upstream, efs-volumes' NFS mount) reports its own class once it
//! has a real adapter.

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum RootEgressClass {
    /// `s3-mounts`: the `mount-s3` daemon, run as the dedicated
    /// `rayito-mount` system user (uid 990, below the M6 IMDS blackhole's
    /// `uidrange 1000-65535`), reads the execution role from IMDS and
    /// calls S3 with it.
    S3,
    /// `rayd-otlp`: OTLP/HTTP metrics exported to `CloudWatch`, `SigV4`-signed
    /// with the execution role.
    CloudwatchOtlp,
    /// `secret-gateway`: the loopback listener's fixed upstream, reached as
    /// root so uid 1000 never sees the vaulted credential.
    SecretGatewayUpstream,
    /// `efs-volumes`: the NFS mount itself (`mount -t efs`), performed as
    /// root like any other mount(2).
    Efs,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_four_classes_are_distinct() {
        let classes = [
            RootEgressClass::S3,
            RootEgressClass::CloudwatchOtlp,
            RootEgressClass::SecretGatewayUpstream,
            RootEgressClass::Efs,
        ];
        for (index, class) in classes.iter().enumerate() {
            for (other_index, other) in classes.iter().enumerate() {
                assert_eq!(class == other, index == other_index);
            }
        }
    }
}
