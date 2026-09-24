//! The SSRF guard of design D6, pure: `rayd` runs as root and reaches IMDS,
//! so it only ever calls a URL that is a `SigV4` presigned request for the
//! exact regional virtual-hosted S3 object the request names, bound by its
//! key to this sandbox and to one direction. Everything is checked before
//! any network I/O; every refusal has a fixed, value-free message. The
//! address predicate the adapter's resolver applies lives here too.

use std::fmt;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

use thiserror::Error;

use super::TRANSFER_URL_MAX_BYTES;

pub const KEY_MAX_BYTES: usize = 1024;
pub const BUCKET_MIN_BYTES: usize = 3;
pub const BUCKET_MAX_BYTES: usize = 63;
/// The only `Content-Type` a presigned request may carry.
pub const OCTET_STREAM: &str = "application/octet-stream";
const TOKEN_HEX_LEN: usize = 32;
const SIGV4_ALGORITHM: &str = "AWS4-HMAC-SHA256";
const REQUIRED_SIGV4_PARAMS: [&str; 5] = [
    "X-Amz-Credential",
    "X-Amz-Date",
    "X-Amz-Expires",
    "X-Amz-SignedHeaders",
    "X-Amz-Signature",
];
const SIGV2_PARAMS: [&str; 2] = ["AWSAccessKeyId", "Signature"];

#[derive(Debug, Error, Clone, Copy, PartialEq, Eq)]
pub enum UrlPolicyError {
    #[error("bucket inválido: nombre DNS sin puntos")]
    Bucket,
    #[error("región inválida")]
    Region,
    #[error("clave inválida")]
    Key,
    #[error("la clave no pertenece a este sandbox o a esta dirección")]
    KeyBinding,
    #[error("la petición no trae las URLs que su dirección necesita")]
    Roles,
    #[error("URL demasiado larga")]
    UrlTooLong,
    #[error("URL mal formada")]
    Malformed,
    #[error("la URL debe usar el esquema seguro")]
    Scheme,
    #[error("la URL no puede llevar credenciales de usuario")]
    UserInfo,
    #[error("la URL sólo puede usar el puerto 443")]
    Port,
    #[error("la URL no puede apuntar a una dirección IP")]
    IpLiteral,
    #[error("el host no es un endpoint regional del bucket")]
    Host,
    #[error("la ruta de la URL no es la clave del objeto")]
    Path,
    #[error("la URL no es una firma SigV4 prefirmada")]
    Signature,
    #[error("las partes no están numeradas en orden o comparten mal el uploadId")]
    PartParameters,
    #[error("las URLs no apuntan al mismo objeto")]
    MixedTargets,
    #[error("cabeceras firmadas no admitidas")]
    Headers,
}

impl UrlPolicyError {
    pub const ALL: [Self; 17] = [
        Self::Bucket,
        Self::Region,
        Self::Key,
        Self::KeyBinding,
        Self::Roles,
        Self::UrlTooLong,
        Self::Malformed,
        Self::Scheme,
        Self::UserInfo,
        Self::Port,
        Self::IpLiteral,
        Self::Host,
        Self::Path,
        Self::Signature,
        Self::PartParameters,
        Self::MixedTargets,
        Self::Headers,
    ];
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TransferDirection {
    Import,
    Export,
}

impl TransferDirection {
    /// The key segment that binds a staging object to one direction.
    #[must_use]
    pub fn key_segment(self) -> &'static str {
        match self {
            Self::Import => "up",
            Self::Export => "down",
        }
    }

    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Import => "import",
            Self::Export => "export",
        }
    }
}

/// The object a request names, after the bucket, region and key rules.
#[derive(Clone, PartialEq, Eq)]
pub struct S3ObjectSpec {
    bucket: String,
    key: String,
    region: String,
}

/// Bucket, key and region are never logged; `Debug` hides them.
impl fmt::Debug for S3ObjectSpec {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("S3ObjectSpec(<redacted>)")
    }
}

impl S3ObjectSpec {
    pub fn parse(bucket: &str, key: &str, region: &str) -> Result<Self, UrlPolicyError> {
        if !is_valid_bucket(bucket) {
            return Err(UrlPolicyError::Bucket);
        }
        if !is_valid_region(region) {
            return Err(UrlPolicyError::Region);
        }
        if !is_valid_key(key) {
            return Err(UrlPolicyError::Key);
        }
        Ok(Self {
            bucket: bucket.to_owned(),
            key: key.to_owned(),
            region: region.to_owned(),
        })
    }

    #[must_use]
    pub fn key(&self) -> &str {
        &self.key
    }

    /// The four virtual-hosted regional endpoints of the bucket; the
    /// global, path-style and accelerate hosts are not among them.
    fn hosts(&self) -> [String; 4] {
        let (bucket, region) = (&self.bucket, &self.region);
        [
            format!("{bucket}.s3.{region}.amazonaws.com"),
            format!("{bucket}.s3.dualstack.{region}.amazonaws.com"),
            format!("{bucket}.s3-fips.{region}.amazonaws.com"),
            format!("{bucket}.s3-fips.dualstack.{region}.amazonaws.com"),
        ]
    }
}

/// What a presigned URL is for inside its request.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UrlRole {
    Get,
    Delete,
    Put,
    /// The `UploadPart` URL of part `number` (1-based).
    Part {
        number: u32,
    },
}

/// One presigned request of a transfer as the policy sees it.
#[derive(Clone, Copy)]
pub struct RequestUrl<'a> {
    pub role: UrlRole,
    pub url: &'a str,
    pub headers: &'a [(String, String)],
}

/// Design D6 rules 1 to 7 over every URL of one request, in order: key
/// binding, the URL set of the direction, then per URL its shape, host,
/// path, signature, part parameters and headers, and finally that they all
/// address the same host.
pub fn validate_request(
    object: &S3ObjectSpec,
    urls: &[RequestUrl<'_>],
    sandbox_id: &str,
    direction: TransferDirection,
) -> Result<(), UrlPolicyError> {
    check_key_binding(&object.key, sandbox_id, direction)?;
    check_roles(urls, direction)?;
    let hosts = object.hosts();
    let mut shared_host: Option<&str> = None;
    let mut upload_id: Option<&str> = None;
    for request in urls {
        let parsed = parse_url(request.url)?;
        if !hosts.iter().any(|host| host == parsed.host) {
            return Err(UrlPolicyError::Host);
        }
        check_path(parsed.path, &object.key)?;
        let params = query_params(parsed.query);
        check_signature(&params)?;
        check_part_parameters(&params, request.role, &mut upload_id)?;
        check_headers(request.headers)?;
        match shared_host {
            Some(host) if host != parsed.host => return Err(UrlPolicyError::MixedTargets),
            Some(_) => {}
            None => shared_host = Some(parsed.host),
        }
    }
    Ok(())
}

/// Addresses the transfer client never connects to: loopback, `0.0.0.0/8`,
/// link-local (IMDS lives there), the IPv6 IMDS address, unspecified,
/// multicast, broadcast, and any IPv4-mapped or IPv4-compatible IPv6
/// address embedding one of those. RFC 1918 and `fc00::/7` stay allowed so
/// S3 interface endpoints keep working.
#[must_use]
pub fn is_forbidden_address(address: IpAddr) -> bool {
    match address {
        IpAddr::V4(v4) => is_forbidden_v4(v4),
        IpAddr::V6(v6) => is_forbidden_v6(v6),
    }
}

fn is_forbidden_v4(address: Ipv4Addr) -> bool {
    address.is_loopback()
        || address.octets()[0] == 0
        || address.is_link_local()
        || address.is_multicast()
        || address.is_broadcast()
}

const IMDS_V6: Ipv6Addr = Ipv6Addr::new(0xfd00, 0x0ec2, 0, 0, 0, 0, 0, 0x0254);

fn is_forbidden_v6(address: Ipv6Addr) -> bool {
    if address.is_loopback() || address.is_unspecified() || address.is_multicast() {
        return true;
    }
    if address.segments()[0] & 0xffc0 == 0xfe80 || address == IMDS_V6 {
        return true;
    }
    address.to_ipv4().is_some_and(is_forbidden_v4)
}

fn is_valid_bucket(bucket: &str) -> bool {
    (BUCKET_MIN_BYTES..=BUCKET_MAX_BYTES).contains(&bucket.len())
        && bucket
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
        && bucket.bytes().next().is_some_and(|byte| byte != b'-')
        && bucket.bytes().next_back().is_some_and(|byte| byte != b'-')
        && !bucket.starts_with("xn--")
        && !bucket.ends_with("-s3alias")
}

/// `^[a-z]{2}(-gov)?-[a-z]+-[0-9]$`
fn is_valid_region(region: &str) -> bool {
    let parts: Vec<&str> = region.split('-').collect();
    let (country, middle, digit) = match parts.as_slice() {
        [country, name, digit] => (country, vec![*name], digit),
        [country, "gov", name, digit] => (country, vec!["gov", *name], digit),
        _ => return false,
    };
    country.len() == 2
        && country.bytes().all(|byte| byte.is_ascii_lowercase())
        && middle
            .last()
            .is_some_and(|name| !name.is_empty() && name.bytes().all(|b| b.is_ascii_lowercase()))
        && digit.len() == 1
        && digit.bytes().all(|byte| byte.is_ascii_digit())
}

fn is_valid_key(key: &str) -> bool {
    (1..=KEY_MAX_BYTES).contains(&key.len())
        && !key.contains('\0')
        && !key.starts_with('/')
        && !key.contains("//")
        && key
            .split('/')
            .all(|segment| segment != "." && segment != "..")
}

/// The key ends with `/<sandbox id>/<up|down>/<32 lowercase hex>` and has
/// at least one segment before that.
fn check_key_binding(
    key: &str,
    sandbox_id: &str,
    direction: TransferDirection,
) -> Result<(), UrlPolicyError> {
    if sandbox_id.is_empty() || sandbox_id.contains('/') {
        return Err(UrlPolicyError::KeyBinding);
    }
    let (rest, token) = key.rsplit_once('/').ok_or(UrlPolicyError::KeyBinding)?;
    let suffix = format!("/{sandbox_id}/{}", direction.key_segment());
    let bound = token.len() == TOKEN_HEX_LEN
        && token
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        && rest.len() > suffix.len()
        && rest.ends_with(&suffix);
    if bound {
        Ok(())
    } else {
        Err(UrlPolicyError::KeyBinding)
    }
}

/// An import is one `Get` plus at most one `Delete`; an export is one
/// `Put`, or parts numbered 1..N in order.
fn check_roles(
    urls: &[RequestUrl<'_>],
    direction: TransferDirection,
) -> Result<(), UrlPolicyError> {
    let roles: Vec<UrlRole> = urls.iter().map(|request| request.role).collect();
    let valid = match direction {
        TransferDirection::Import => matches!(
            roles.as_slice(),
            [UrlRole::Get] | [UrlRole::Get, UrlRole::Delete]
        ),
        TransferDirection::Export => {
            matches!(roles.as_slice(), [UrlRole::Put])
                || (!roles.is_empty()
                    && roles.iter().enumerate().all(|(index, role)| {
                        u32::try_from(index + 1)
                            .is_ok_and(|expected| *role == UrlRole::Part { number: expected })
                    }))
        }
    };
    if valid {
        Ok(())
    } else {
        Err(UrlPolicyError::Roles)
    }
}

struct ParsedUrl<'a> {
    host: &'a str,
    path: &'a str,
    query: &'a str,
}

/// A strict reading of `https://host[:443]/path?query`: visible ASCII only,
/// no fragment, no userinfo, no port but 443, no IP literal.
fn parse_url(url: &str) -> Result<ParsedUrl<'_>, UrlPolicyError> {
    if url.len() > TRANSFER_URL_MAX_BYTES {
        return Err(UrlPolicyError::UrlTooLong);
    }
    if !url.bytes().all(|byte| (0x21..=0x7e).contains(&byte)) || url.contains('#') {
        return Err(UrlPolicyError::Malformed);
    }
    let (scheme, rest) = url.split_once("://").ok_or(UrlPolicyError::Malformed)?;
    if scheme != "https" {
        return Err(UrlPolicyError::Scheme);
    }
    let authority_end = rest.find(['/', '?']).unwrap_or(rest.len());
    let (authority, tail) = rest.split_at(authority_end);
    if authority.is_empty() {
        return Err(UrlPolicyError::Malformed);
    }
    if authority.contains('@') {
        return Err(UrlPolicyError::UserInfo);
    }
    let host = host_without_port(authority)?;
    if is_ip_literal(host) {
        return Err(UrlPolicyError::IpLiteral);
    }
    let (path, query) = tail.split_once('?').unwrap_or((tail, ""));
    Ok(ParsedUrl { host, path, query })
}

fn host_without_port(authority: &str) -> Result<&str, UrlPolicyError> {
    if authority.starts_with('[') {
        return Err(UrlPolicyError::IpLiteral);
    }
    match authority.rsplit_once(':') {
        Some((host, "443")) if !host.is_empty() => Ok(host),
        Some(_) => Err(UrlPolicyError::Port),
        None => Ok(authority),
    }
}

fn is_ip_literal(host: &str) -> bool {
    host.parse::<Ipv4Addr>().is_ok()
        || host
            .bytes()
            .all(|byte| byte.is_ascii_digit() || byte == b'.')
        || host.contains(':')
}

/// The percent-decoded path is `/` + key, byte for byte.
fn check_path(path: &str, key: &str) -> Result<(), UrlPolicyError> {
    let decoded = percent_decode(path).ok_or(UrlPolicyError::Path)?;
    let expected = format!("/{key}");
    if decoded == expected.as_bytes() {
        Ok(())
    } else {
        Err(UrlPolicyError::Path)
    }
}

/// `None` on an invalid escape or a decoded NUL.
fn percent_decode(raw: &str) -> Option<Vec<u8>> {
    let bytes = raw.as_bytes();
    let mut decoded = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        let byte = if bytes[index] == b'%' {
            let high = hex_value(*bytes.get(index + 1)?)?;
            let low = hex_value(*bytes.get(index + 2)?)?;
            index += 3;
            (high << 4) | low
        } else {
            index += 1;
            bytes[index - 1]
        };
        if byte == 0 {
            return None;
        }
        decoded.push(byte);
    }
    Some(decoded)
}

fn hex_value(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

fn query_params(query: &str) -> Vec<(&str, &str)> {
    query
        .split('&')
        .filter(|param| !param.is_empty())
        .map(|param| param.split_once('=').unwrap_or((param, "")))
        .collect()
}

fn values<'a>(params: &[(&'a str, &'a str)], name: &str) -> Vec<&'a str> {
    params
        .iter()
        .filter(|(param, _)| *param == name)
        .map(|(_, value)| *value)
        .collect()
}

/// Exactly one `X-Amz-Algorithm=AWS4-HMAC-SHA256` and one non-empty value
/// of each other `SigV4` parameter; no `SigV2` parameter at all.
fn check_signature(params: &[(&str, &str)]) -> Result<(), UrlPolicyError> {
    let algorithm_ok = values(params, "X-Amz-Algorithm") == [SIGV4_ALGORITHM];
    let required_ok = REQUIRED_SIGV4_PARAMS.iter().all(|name| {
        let found = values(params, name);
        found.len() == 1 && !found[0].is_empty()
    });
    let sigv2 = SIGV2_PARAMS
        .iter()
        .any(|name| !values(params, name).is_empty());
    if algorithm_ok && required_ok && !sigv2 {
        Ok(())
    } else {
        Err(UrlPolicyError::Signature)
    }
}

/// An `UploadPart` URL carries `partNumber` equal to its position and the
/// `uploadId` every other part carries; any other URL carries neither (a
/// `GET` with `partNumber` would import a fragment, a `DELETE` with
/// `uploadId` is a different operation).
fn check_part_parameters<'a>(
    params: &[(&'a str, &'a str)],
    role: UrlRole,
    upload_id: &mut Option<&'a str>,
) -> Result<(), UrlPolicyError> {
    let part_numbers = values(params, "partNumber");
    let upload_ids = values(params, "uploadId");
    let UrlRole::Part { number } = role else {
        return if part_numbers.is_empty() && upload_ids.is_empty() {
            Ok(())
        } else {
            Err(UrlPolicyError::PartParameters)
        };
    };
    let expected_number = number.to_string();
    let valid = part_numbers == [expected_number.as_str()]
        && upload_ids.len() == 1
        && !upload_ids[0].is_empty()
        && upload_id.is_none_or(|shared| shared == upload_ids[0]);
    if !valid {
        return Err(UrlPolicyError::PartParameters);
    }
    *upload_id = Some(upload_ids[0]);
    Ok(())
}

/// Empty, or only `content-type: application/octet-stream`.
fn check_headers(headers: &[(String, String)]) -> Result<(), UrlPolicyError> {
    let valid = match headers {
        [] => true,
        [(name, value)] => name.eq_ignore_ascii_case("content-type") && value == OCTET_STREAM,
        _ => false,
    };
    if valid {
        Ok(())
    } else {
        Err(UrlPolicyError::Headers)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SANDBOX: &str = "microvm-0abc";
    const TOKEN: &str = "0123456789abcdef0123456789abcdef";
    const QUERY: &str = "X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKIA%2F20260922%2Fus-east-1%2Fs3%2Faws4_request&X-Amz-Date=20260922T000000Z&X-Amz-Expires=3600&X-Amz-SignedHeaders=host&X-Amz-Signature=abcdef";

    fn up_key() -> String {
        format!("rayito-transfer/{SANDBOX}/up/{TOKEN}")
    }

    fn down_key() -> String {
        format!("rayito-transfer/{SANDBOX}/down/{TOKEN}")
    }

    fn object(key: &str) -> S3ObjectSpec {
        S3ObjectSpec::parse("amzn-s3-demo-bucket", key, "us-east-1").unwrap()
    }

    fn url(host: &str, key: &str, extra: &str) -> String {
        format!("https://{host}/{key}?{QUERY}{extra}")
    }

    fn regional(key: &str) -> String {
        url("amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com", key, "")
    }

    fn import(get: &str) -> Result<(), UrlPolicyError> {
        import_with(&up_key(), get, None, &[])
    }

    fn import_with(
        key: &str,
        get: &str,
        delete: Option<&str>,
        headers: &[(String, String)],
    ) -> Result<(), UrlPolicyError> {
        let mut urls = vec![RequestUrl {
            role: UrlRole::Get,
            url: get,
            headers,
        }];
        if let Some(delete) = delete {
            urls.push(RequestUrl {
                role: UrlRole::Delete,
                url: delete,
                headers: &[],
            });
        }
        validate_request(&object(key), &urls, SANDBOX, TransferDirection::Import)
    }

    fn parts(urls: &[String]) -> Result<(), UrlPolicyError> {
        let requests: Vec<RequestUrl<'_>> = urls
            .iter()
            .enumerate()
            .map(|(index, url)| RequestUrl {
                role: UrlRole::Part {
                    number: u32::try_from(index + 1).unwrap(),
                },
                url,
                headers: &[],
            })
            .collect();
        validate_request(
            &object(&down_key()),
            &requests,
            SANDBOX,
            TransferDirection::Export,
        )
    }

    fn part_url(number: u32, upload: &str) -> String {
        url(
            "amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com",
            &down_key(),
            &format!("&partNumber={number}&uploadId={upload}"),
        )
    }

    #[test]
    fn the_four_regional_host_forms_are_accepted() {
        let key = up_key();
        for host in [
            "amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com",
            "amzn-s3-demo-bucket.s3.dualstack.us-east-1.amazonaws.com",
            "amzn-s3-demo-bucket.s3-fips.us-east-1.amazonaws.com",
            "amzn-s3-demo-bucket.s3-fips.dualstack.us-east-1.amazonaws.com",
            "amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com:443",
        ] {
            assert_eq!(import(&url(host, &key, "")), Ok(()), "{host}");
        }
        let get = regional(&key);
        assert_eq!(import_with(&key, &get, Some(&get), &[]), Ok(()));
        let octet = vec![("Content-Type".to_owned(), OCTET_STREAM.to_owned())];
        assert_eq!(import_with(&key, &get, None, &octet), Ok(()));
    }

    #[test]
    fn hosts_other_than_the_regional_endpoints_are_refused() {
        let key = up_key();
        let cases = [
            (
                "http://amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com",
                UrlPolicyError::Scheme,
            ),
            (
                "HTTPS://amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com",
                UrlPolicyError::Scheme,
            ),
            ("https://169.254.169.254", UrlPolicyError::IpLiteral),
            ("https://10.0.0.5", UrlPolicyError::IpLiteral),
            ("https://[::1]", UrlPolicyError::IpLiteral),
            (
                "https://amzn-s3-demo-bucket.s3.amazonaws.com",
                UrlPolicyError::Host,
            ),
            (
                "https://s3.us-east-1.amazonaws.com/amzn-s3-demo-bucket",
                UrlPolicyError::Host,
            ),
            (
                "https://amzn-s3-demo-bucket.s3-accelerate.amazonaws.com",
                UrlPolicyError::Host,
            ),
            (
                "https://other-bucket.s3.us-east-1.amazonaws.com",
                UrlPolicyError::Host,
            ),
            (
                "https://amzn-s3-demo-bucket.s3.us-west-2.amazonaws.com",
                UrlPolicyError::Host,
            ),
            (
                "https://AMZN-S3-DEMO-BUCKET.s3.us-east-1.amazonaws.com",
                UrlPolicyError::Host,
            ),
            (
                "https://user:pw@amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com",
                UrlPolicyError::UserInfo,
            ),
            (
                "https://amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com:8443",
                UrlPolicyError::Port,
            ),
            (
                "https://amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com:",
                UrlPolicyError::Port,
            ),
        ];
        for (base, expected) in cases {
            let full = format!("{base}/{key}?{QUERY}");
            assert_eq!(import(&full), Err(expected), "{base}");
        }
    }

    #[test]
    fn the_path_must_decode_to_the_key() {
        let key = up_key();
        let host = "https://amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com";
        let encoded = key.replace('/', "%2F");
        assert_eq!(import(&format!("{host}/{encoded}?{QUERY}")), Ok(()));
        for path in [
            format!("/{key}x"),
            format!("/other/{key}"),
            format!("//{key}"),
            format!("/{}", key.replace("rayito", "ray%00ito")),
            format!("/{}", key.replace("rayito", "ray%zzito")),
            format!("/{}", key.replace("rayito", "ray%2")),
            String::new(),
        ] {
            assert_eq!(
                import(&format!("{host}{path}?{QUERY}")),
                Err(UrlPolicyError::Path),
                "{path}"
            );
        }
    }

    #[test]
    fn only_sigv4_presigned_queries_pass() {
        let key = up_key();
        let host = "https://amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com";
        let sigv2 = format!("{host}/{key}?AWSAccessKeyId=AKIA&Expires=1&Signature=abc");
        assert_eq!(import(&sigv2), Err(UrlPolicyError::Signature));
        let missing = format!(
            "{host}/{key}?{}",
            QUERY.replace("&X-Amz-Signature=abcdef", "")
        );
        assert_eq!(import(&missing), Err(UrlPolicyError::Signature));
        let empty_signature = format!("{host}/{key}?{}", QUERY.replace("=abcdef", "="));
        assert_eq!(import(&empty_signature), Err(UrlPolicyError::Signature));
        let doubled = url(
            "amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com",
            &key,
            "&X-Amz-Signature=other",
        );
        assert_eq!(import(&doubled), Err(UrlPolicyError::Signature));
        let hmac_sha1 = format!("{host}/{key}?{}", QUERY.replace("SHA256", "SHA1"));
        assert_eq!(import(&hmac_sha1), Err(UrlPolicyError::Signature));
        let mixed = url(
            "amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com",
            &key,
            "&Signature=abc",
        );
        assert_eq!(import(&mixed), Err(UrlPolicyError::Signature));
        let with_token = url(
            "amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com",
            &key,
            "&X-Amz-Security-Token=tok&x-id=GetObject",
        );
        assert_eq!(import(&with_token), Ok(()));
    }

    #[test]
    fn malformed_and_oversized_urls_are_refused() {
        let key = up_key();
        let long = url(
            "amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com",
            &key,
            &format!("&pad={}", "a".repeat(TRANSFER_URL_MAX_BYTES)),
        );
        assert_eq!(import(&long), Err(UrlPolicyError::UrlTooLong));
        for bad in [
            format!("{} ", regional(&key)),
            format!("{}#frag", regional(&key)),
            "amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com".to_owned(),
            format!("https:///{key}?{QUERY}"),
            format!("{}ñ", regional(&key)),
        ] {
            assert_eq!(import(&bad), Err(UrlPolicyError::Malformed), "{bad}");
        }
    }

    #[test]
    fn keys_are_bound_to_the_sandbox_and_the_direction() {
        let host = "amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com";
        for key in [
            format!("rayito-transfer/microvm-other/up/{TOKEN}"),
            format!("rayito-transfer/{SANDBOX}/down/{TOKEN}"),
            format!("rayito-transfer/{SANDBOX}/up/{}", TOKEN.to_uppercase()),
            format!("rayito-transfer/{SANDBOX}/up/{}", &TOKEN[1..]),
            format!("{SANDBOX}/up/{TOKEN}"),
        ] {
            let get = url(host, &key, "");
            assert_eq!(
                import_with(&key, &get, None, &[]),
                Err(UrlPolicyError::KeyBinding),
                "{key}"
            );
        }
        let mismatched = url(
            host,
            &format!("rayito-transfer/{SANDBOX}/up/{}", "f".repeat(32)),
            "",
        );
        assert_eq!(import(&mismatched), Err(UrlPolicyError::Path));
    }

    #[test]
    fn object_rules_refuse_dotted_buckets_bad_regions_and_bad_keys() {
        let key = up_key();
        for bucket in [
            "my.bucket",
            "ab",
            "-bucket",
            "bucket-",
            "Bucket",
            "xn--bucket",
            "bucket-s3alias",
            "bucket_1",
        ] {
            assert_eq!(
                S3ObjectSpec::parse(bucket, &key, "us-east-1"),
                Err(UrlPolicyError::Bucket),
                "{bucket}"
            );
        }
        for region in [
            "us-east",
            "useast-1",
            "US-EAST-1",
            "us-east-12",
            "u-east-1",
            "",
        ] {
            assert_eq!(
                S3ObjectSpec::parse("amzn-s3-demo-bucket", &key, region),
                Err(UrlPolicyError::Region),
                "{region}"
            );
        }
        assert!(S3ObjectSpec::parse("amzn-s3-demo-bucket", &key, "us-gov-west-1").is_ok());
        for bad_key in [
            String::new(),
            format!("/{key}"),
            key.replace("/up/", "//up/"),
            key.replace("rayito-transfer", "."),
            key.replace("rayito-transfer", ".."),
            format!("{key}\0"),
            "k".repeat(KEY_MAX_BYTES + 1),
        ] {
            assert_eq!(
                S3ObjectSpec::parse("amzn-s3-demo-bucket", &bad_key, "us-east-1"),
                Err(UrlPolicyError::Key),
                "{bad_key:?}"
            );
        }
        assert_eq!(format!("{:?}", object(&key)), "S3ObjectSpec(<redacted>)");
    }

    #[test]
    fn every_url_of_a_request_addresses_the_same_object() {
        let key = up_key();
        let get = regional(&key);
        let dualstack = url(
            "amzn-s3-demo-bucket.s3.dualstack.us-east-1.amazonaws.com",
            &key,
            "",
        );
        assert_eq!(
            import_with(&key, &get, Some(&dualstack), &[]),
            Err(UrlPolicyError::MixedTargets)
        );
        let other_key = regional(&up_key().replace("rayito-transfer", "elsewhere"));
        assert_eq!(
            import_with(&key, &get, Some(&other_key), &[]),
            Err(UrlPolicyError::Path)
        );
    }

    #[test]
    fn only_the_octet_stream_content_type_header_is_accepted() {
        let key = up_key();
        let get = regional(&key);
        for headers in [
            vec![("content-type".to_owned(), "text/plain".to_owned())],
            vec![("x-amz-checksum-crc32".to_owned(), "AAAA".to_owned())],
            vec![
                ("content-type".to_owned(), OCTET_STREAM.to_owned()),
                ("content-type".to_owned(), OCTET_STREAM.to_owned()),
            ],
        ] {
            assert_eq!(
                import_with(&key, &get, None, &headers),
                Err(UrlPolicyError::Headers)
            );
        }
    }

    #[test]
    fn upload_parts_are_numbered_in_order_and_share_one_upload_id() {
        assert_eq!(parts(&[part_url(1, "U1"), part_url(2, "U1")]), Ok(()));
        assert_eq!(
            parts(&[part_url(1, "U1"), part_url(3, "U1")]),
            Err(UrlPolicyError::PartParameters)
        );
        assert_eq!(
            parts(&[part_url(1, "U1"), part_url(2, "U2")]),
            Err(UrlPolicyError::PartParameters)
        );
        let without_upload = url(
            "amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com",
            &down_key(),
            "&partNumber=1",
        );
        assert_eq!(
            parts(&[without_upload]),
            Err(UrlPolicyError::PartParameters)
        );
        let get_with_part = url(
            "amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com",
            &up_key(),
            "&partNumber=1",
        );
        assert_eq!(import(&get_with_part), Err(UrlPolicyError::PartParameters));
        assert_eq!(parts(&[]), Err(UrlPolicyError::Roles));
    }

    #[test]
    fn requests_carry_exactly_the_urls_of_their_direction() {
        let get = regional(&up_key());
        let put = regional(&down_key());
        let object = object(&down_key());
        let as_put = [RequestUrl {
            role: UrlRole::Put,
            url: &put,
            headers: &[],
        }];
        assert_eq!(
            validate_request(&object, &as_put, SANDBOX, TransferDirection::Export),
            Ok(())
        );
        let two_puts = [as_put[0], as_put[0]];
        assert_eq!(
            validate_request(&object, &two_puts, SANDBOX, TransferDirection::Export),
            Err(UrlPolicyError::Roles)
        );
        let delete_only = [RequestUrl {
            role: UrlRole::Delete,
            url: &get,
            headers: &[],
        }];
        assert_eq!(
            validate_request(
                &self::object(&up_key()),
                &delete_only,
                SANDBOX,
                TransferDirection::Import
            ),
            Err(UrlPolicyError::Roles)
        );
        assert_eq!(
            validate_request(&object, &as_put, "", TransferDirection::Export),
            Err(UrlPolicyError::KeyBinding)
        );
    }

    #[test]
    fn forbidden_addresses_cover_loopback_link_local_imds_and_mapped_forms() {
        let forbidden = [
            "127.0.0.1",
            "127.255.0.1",
            "0.0.0.0",
            "0.1.2.3",
            "169.254.169.254",
            "169.254.0.1",
            "224.0.0.1",
            "239.255.255.255",
            "255.255.255.255",
            "::1",
            "::",
            "fe80::1",
            "febf::1",
            "fd00:ec2::254",
            "ff02::1",
            "::ffff:127.0.0.1",
            "::ffff:169.254.169.254",
            "::169.254.169.254",
        ];
        for address in forbidden {
            assert!(is_forbidden_address(address.parse().unwrap()), "{address}");
        }
        let allowed = [
            "10.0.0.5",
            "172.16.0.1",
            "192.168.1.1",
            "52.216.0.1",
            "fd00:ec2::253",
            "fc00::1",
            "2600:1f18::1",
            "::ffff:52.216.0.1",
            "fec0::1",
        ];
        for address in allowed {
            assert!(!is_forbidden_address(address.parse().unwrap()), "{address}");
        }
    }
}
