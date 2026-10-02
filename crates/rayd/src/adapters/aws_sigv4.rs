//! AWS Signature Version 4 for one fixed request shape: a `POST` with a
//! binary body to a single AWS service endpoint (m15-rayd-otlp's
//! `cloudwatch_otlp_sink`, service `monitoring`). `aws-sigv4` is a public
//! crate and already in `Cargo.lock` (pulled in transitively by
//! `aws-sdk-s3`), but the M15 shared-file protocol allows this feature to
//! add vendored protos, not new *direct* crates (§5), so HMAC-SHA256 is
//! built here from the `sha2` dependency already in the workspace
//! (RFC 2104), verified against the RFC 4231 test vector below and against
//! the official `SigV4` test suite's `post-vanilla` vector further down.
//!
//! Pure: no socket, no clock call (`timestamp` is a parameter), so every
//! byte of what gets signed is a unit-testable function of its inputs.
//! Credential values are never `Debug`/`Display`-derived onto this module's
//! own types; callers already hold them in `Zeroizing` (`GuestCredentials`,
//! `PushedCredentials`) and pass plain `&str` in only for the signing
//! computation's lifetime.

use std::fmt::Write as _;

use sha2::{Digest, Sha256};

use rayd_core::transfer::lower_hex;

/// SHA-256's block size (RFC 2104 §2): the HMAC key is zero-padded to this
/// length, or hashed down to it first if longer.
const HMAC_BLOCK_SIZE: usize = 64;
const IPAD: u8 = 0x36;
const OPAD: u8 = 0x5c;
/// AWS `SigV4`'s fixed credential-scope suffix (`SigV4-signing.html`).
const TERMINATOR: &str = "aws4_request";
const ALGORITHM: &str = "AWS4-HMAC-SHA256";

/// What `sign` needs: everything about one request that the signature
/// covers. `path` is already percent-encoded; this module adds no query
/// string (`CloudWatch`'s OTLP endpoint takes none).
pub struct SigningRequest<'a> {
    pub method: &'static str,
    pub host: &'a str,
    pub path: &'a str,
    pub payload: &'a [u8],
    pub content_type: &'a str,
    pub region: &'a str,
    pub service: &'a str,
    pub access_key_id: &'a str,
    pub secret_access_key: &'a str,
    pub session_token: Option<&'a str>,
    pub timestamp_unix: u64,
}

/// The headers `sign` computes; the caller (`cloudwatch_otlp_sink`) is the
/// one that actually opens the connection and sets them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SignedHeaders {
    pub x_amz_date: String,
    pub x_amz_content_sha256: String,
    pub authorization: String,
}

#[must_use]
pub fn sign(request: &SigningRequest<'_>) -> SignedHeaders {
    let (date_stamp, amz_date) = format_timestamp(request.timestamp_unix);
    let payload_hash = lower_hex(&sha256(request.payload));
    let signed_header_names = signed_header_names(request.session_token.is_some());
    let canonical_request =
        canonical_request(request, &amz_date, &payload_hash, signed_header_names);
    let credential_scope = format!(
        "{date_stamp}/{}/{}/{TERMINATOR}",
        request.region, request.service
    );
    let string_to_sign = format!(
        "{ALGORITHM}\n{amz_date}\n{credential_scope}\n{}",
        lower_hex(&sha256(canonical_request.as_bytes()))
    );
    let signing_key = derive_signing_key(
        request.secret_access_key,
        &date_stamp,
        request.region,
        request.service,
    );
    let signature = lower_hex(&hmac_sha256(&signing_key, string_to_sign.as_bytes()));
    let authorization = format!(
        "{ALGORITHM} Credential={}/{credential_scope}, SignedHeaders={signed_header_names}, Signature={signature}",
        request.access_key_id,
    );
    SignedHeaders {
        x_amz_date: amz_date,
        x_amz_content_sha256: payload_hash,
        authorization,
    }
}

/// `host`, `x-amz-date`, `x-amz-content-sha256` and, when present,
/// `x-amz-security-token`: the minimal signed-header set for a request
/// whose only other header (`content-type`) is fixed and never varies
/// per-request, so excluding it does not weaken the signature's binding to
/// what this module actually sends (`SigV4` only requires `host` and the
/// `x-amz-*` headers to be signed; signing more is stricter, never looser).
fn signed_header_names(has_session_token: bool) -> &'static str {
    if has_session_token {
        "host;x-amz-content-sha256;x-amz-date;x-amz-security-token"
    } else {
        "host;x-amz-content-sha256;x-amz-date"
    }
}

fn canonical_request(
    request: &SigningRequest<'_>,
    amz_date: &str,
    payload_hash: &str,
    signed_header_names: &str,
) -> String {
    let mut canonical_headers = format!(
        "host:{}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n",
        request.host
    );
    if let Some(token) = request.session_token {
        // `write!` into a `String` never fails; the lints deny `unwrap`, so
        // the `Result` is deliberately discarded rather than asserted on.
        let _ = writeln!(canonical_headers, "x-amz-security-token:{token}");
    }
    format!(
        "{}\n{}\n\n{canonical_headers}\n{signed_header_names}\n{payload_hash}",
        request.method, request.path,
    )
}

fn derive_signing_key(secret: &str, date_stamp: &str, region: &str, service: &str) -> [u8; 32] {
    let k_date = hmac_sha256(format!("AWS4{secret}").as_bytes(), date_stamp.as_bytes());
    let k_region = hmac_sha256(&k_date, region.as_bytes());
    let k_service = hmac_sha256(&k_region, service.as_bytes());
    hmac_sha256(&k_service, TERMINATOR.as_bytes())
}

/// `(YYYYMMDD, YYYYMMDDTHHMMSSZ)`: both derived from the same Unix
/// timestamp so the date stamp in the credential scope always matches the
/// day of the signed `x-amz-date`, even exactly at midnight UTC.
fn format_timestamp(unix_seconds: u64) -> (String, String) {
    const SECONDS_PER_DAY: u64 = 86_400;
    let days_since_epoch = unix_seconds / SECONDS_PER_DAY;
    let seconds_of_day = unix_seconds % SECONDS_PER_DAY;
    let (year, month, day) = civil_from_days(i64::try_from(days_since_epoch).unwrap_or(0));
    let hour = seconds_of_day / 3600;
    let minute = (seconds_of_day % 3600) / 60;
    let second = seconds_of_day % 60;
    let date_stamp = format!("{year:04}{month:02}{day:02}");
    let amz_date = format!("{date_stamp}T{hour:02}{minute:02}{second:02}Z");
    (date_stamp, amz_date)
}

/// Howard Hinnant's `civil_from_days` (public domain,
/// <https://howardhinnant.github.io/date_algorithms.html>): days since the
/// Unix epoch to a proleptic-Gregorian `(year, month, day)`, with no
/// calendar crate (the one date computation this feature needs).
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    // Always non-negative by construction of `era` (the classic 0..=146_096
    // era-local day count), so the sign-changing cast is exact, never a
    // truncation; the `u64 -> u32` narrowings below are bounded the same
    // way (a day-of-year and a 1-based month/day both fit easily in `u32`),
    // so `unwrap_or` here is dead code, not a silent wraparound.
    let doe = (z - era * 146_097).cast_unsigned();
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe.cast_signed() + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = u32::try_from(doy - (153 * mp + 2) / 5 + 1).unwrap_or(u32::MAX);
    let month = u32::try_from(if mp < 10 { mp + 3 } else { mp - 9 }).unwrap_or(u32::MAX);
    let year = if month <= 2 { y + 1 } else { y };
    (year, month, day)
}

fn sha256(data: &[u8]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(data);
    hasher.finalize().into()
}

/// RFC 2104 HMAC over SHA-256.
fn hmac_sha256(key: &[u8], message: &[u8]) -> [u8; 32] {
    let mut block_key = [0u8; HMAC_BLOCK_SIZE];
    if key.len() > HMAC_BLOCK_SIZE {
        block_key[..32].copy_from_slice(&sha256(key));
    } else {
        block_key[..key.len()].copy_from_slice(key);
    }
    let mut ipad = [IPAD; HMAC_BLOCK_SIZE];
    let mut opad = [OPAD; HMAC_BLOCK_SIZE];
    for index in 0..HMAC_BLOCK_SIZE {
        ipad[index] ^= block_key[index];
        opad[index] ^= block_key[index];
    }
    let mut inner = Vec::with_capacity(HMAC_BLOCK_SIZE + message.len());
    inner.extend_from_slice(&ipad);
    inner.extend_from_slice(message);
    let inner_hash = sha256(&inner);
    let mut outer = Vec::with_capacity(HMAC_BLOCK_SIZE + inner_hash.len());
    outer.extend_from_slice(&opad);
    outer.extend_from_slice(&inner_hash);
    sha256(&outer)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Empty-string SHA-256, famous enough to double as a regression check
    /// on `sha256`/`lower_hex` together (also used elsewhere in this repo,
    /// e.g. `grpc::configure`'s tests, as a known-good payload hash).
    const EMPTY_SHA256: &str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

    #[test]
    fn sha256_of_the_empty_string_is_the_well_known_value() {
        assert_eq!(lower_hex(&sha256(b"")), EMPTY_SHA256);
    }

    /// RFC 4231 §4.2, HMAC-SHA-256 test case 1.
    #[test]
    fn hmac_sha256_matches_the_rfc_4231_test_vector() {
        let key = [0x0bu8; 20];
        let mac = hmac_sha256(&key, b"Hi There");
        assert_eq!(
            lower_hex(&mac),
            "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"
        );
    }

    #[test]
    fn hmac_sha256_also_handles_a_key_longer_than_one_block() {
        // A 131-byte key is longer than SHA-256's 64-byte block, so
        // `hmac_sha256` must hash it down first (RFC 2104 §2) rather than
        // zero-pad or truncate it; this only checks that branch is taken
        // deterministically and produces a full-width digest, not a
        // specific hash (no independently-verifiable oracle value is
        // hardcoded here — RFC 4231 test case 1 above already pins the
        // core algorithm against a known answer).
        let key = [0xaau8; 131];
        let message = b"Test Using Larger Than Block-Size Key - Hash Key First";
        let mac = hmac_sha256(&key, message);
        assert_eq!(mac.len(), 32);
        assert_eq!(mac, hmac_sha256(&key, message));
        assert_ne!(mac, hmac_sha256(&key, b"a different message"));
    }

    #[test]
    fn the_epoch_second_is_1970_01_01t00_00_00z() {
        let (date_stamp, amz_date) = format_timestamp(0);
        assert_eq!(date_stamp, "19700101");
        assert_eq!(amz_date, "19700101T000000Z");
    }

    #[test]
    fn a_known_timestamp_formats_as_the_aws_examples_show() {
        // 2015-08-30T12:36:00Z, straight out of AWS's own `SigV4` worked
        // example (`sigv4-create-canonical-request.html`).
        let (date_stamp, amz_date) = format_timestamp(1_440_938_160);
        assert_eq!(date_stamp, "20150830");
        assert_eq!(amz_date, "20150830T123600Z");
    }

    fn sample_request(timestamp_unix: u64) -> SigningRequest<'static> {
        SigningRequest {
            method: "POST",
            host: "monitoring.us-east-1.amazonaws.com",
            path: "/v1/metrics",
            payload: b"",
            content_type: "application/x-protobuf",
            region: "us-east-1",
            service: "monitoring",
            access_key_id: "AKIDEXAMPLE",
            secret_access_key: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            session_token: None,
            timestamp_unix,
        }
    }

    #[test]
    fn signing_is_deterministic_for_the_same_inputs() {
        let request = sample_request(1_440_938_160);
        assert_eq!(sign(&request), sign(&request));
    }

    #[test]
    fn the_payload_hash_header_matches_the_actual_payload() {
        let signed = sign(&sample_request(1_440_938_160));
        assert_eq!(signed.x_amz_content_sha256, EMPTY_SHA256);
        assert_eq!(signed.x_amz_date, "20150830T123600Z");
    }

    #[test]
    fn the_authorization_header_names_the_access_key_scope_and_signed_headers() {
        let signed = sign(&sample_request(1_440_938_160));
        assert!(signed.authorization.starts_with(
            "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/monitoring/aws4_request, \
             SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature="
        ));
    }

    #[test]
    fn a_session_token_is_folded_into_the_signed_headers() {
        let mut request = sample_request(1_440_938_160);
        request.session_token = Some("FQoGZXIvYXdz...");
        let signed = sign(&request);
        assert!(
            signed.authorization.contains(
                "SignedHeaders=host;x-amz-content-sha256;x-amz-date;x-amz-security-token"
            )
        );
    }

    #[test]
    fn changing_the_payload_changes_the_signature() {
        let mut first = sample_request(1_440_938_160);
        first.payload = b"a";
        let mut second = sample_request(1_440_938_160);
        second.payload = b"b";
        assert_ne!(sign(&first).authorization, sign(&second).authorization);
    }

    /// The official AWS `SigV4` test suite's `post-vanilla` case (`AKIDEXAMPLE`,
    /// confirmed from `aws/aws-cli` and `boto/botocore`'s own copies of
    /// `tests/unit/botocore/auth/aws4_testsuite/post-vanilla/*`, the same
    /// suite `aws-c-auth`'s `aws-sig-v4-test-suite` ships): a third-party,
    /// independently-published fixture, not just this module agreeing with
    /// itself. Its header set (`host;x-amz-date`, no `x-amz-content-sha256`)
    /// differs from the one fixed header set `sign`/`canonical_request`
    /// ever build, so this pins the lower-level primitives every header set
    /// shares (`sha256`, `derive_signing_key`, `hmac_sha256`) directly,
    /// bypassing `canonical_request`, rather than claiming `sign` itself
    /// reproduces a request shape it cannot build.
    ///
    /// NOTE: the suite's documented secret is
    /// `wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY` (a `+`, confirmed from
    /// `botocore`'s `tests/unit/auth/test_sigv4.py`), one character off
    /// from the `/`-variant AWS's own worked canonical-request example
    /// uses (`sample_request` above) -- two different well-known
    /// `AKIDEXAMPLE` fixtures that must not be swapped.
    #[test]
    fn matches_the_official_aws_sigv4_test_suite_post_vanilla_vector() {
        const SECRET: &str = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY";
        const CANONICAL_REQUEST: &str = "POST\n/\n\nhost:example.amazonaws.com\n\
             x-amz-date:20150830T123600Z\n\nhost;x-amz-date\n\
             e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
        const EXPECTED_HASHED_CANONICAL_REQUEST: &str =
            "553f88c9e4d10fc9e109e2aeb65f030801b70c2f6468faca261d401ae622fc87";
        const EXPECTED_SIGNATURE: &str =
            "5da7c1a2acd57cee7505fc6676e4e544621c30862966e37dddb68e92efbe5d6b";

        let hashed_canonical_request = lower_hex(&sha256(CANONICAL_REQUEST.as_bytes()));
        assert_eq!(hashed_canonical_request, EXPECTED_HASHED_CANONICAL_REQUEST);
        let string_to_sign = format!(
            "AWS4-HMAC-SHA256\n20150830T123600Z\n20150830/us-east-1/service/aws4_request\n{hashed_canonical_request}"
        );
        let signing_key = derive_signing_key(SECRET, "20150830", "us-east-1", "service");
        let signature = lower_hex(&hmac_sha256(&signing_key, string_to_sign.as_bytes()));
        assert_eq!(signature, EXPECTED_SIGNATURE);
    }

    #[test]
    fn changing_the_secret_changes_the_signature_but_not_the_access_key_in_the_header() {
        let first = sample_request(1_440_938_160);
        let mut second = sample_request(1_440_938_160);
        second.secret_access_key = "a-different-secret";
        let a = sign(&first);
        let b = sign(&second);
        assert_ne!(a.authorization, b.authorization);
        assert!(a.authorization.contains("AKIDEXAMPLE"));
        assert!(b.authorization.contains("AKIDEXAMPLE"));
    }
}
