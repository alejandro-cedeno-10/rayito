//! Dominio puro de m15-templates (investigación §3.5): el `StartSpec` que
//! `rayd` lee de `/etc/rayito/template.json` (`rayito.template/1`, horneado
//! por `Template.build()`/`AsyncTemplate.build()` del SDK) y la decisión de
//! `/ready` mientras el `ready_cmd` no haya salido con 0. Nada aquí abre un
//! fichero ni arranca un proceso: `rayd::adapters::fs_template_spec` lee el
//! fichero (puerto `TemplateSpecSource`), `rayd::adapters::shell_ready_probe`
//! ejecuta el sondeo (puerto `ReadyProbe`), y
//! `rayd::lifecycle::template_participant` conecta esto con
//! `LifecycleParticipant::ready_gate` (ADR-015: `Fail` y `Retry` responden
//! igual en `/ready`, 503, así que esta decisión de dominio distingue ambos
//! casos para cuando ese combinador gane un tercer estado, pero hoy no
//! cambia el código HTTP que ve AWS).

use std::collections::BTreeMap;
use std::time::Duration;

use serde::{Deserialize, Serialize};

/// Marca de versión del `/etc/rayito/template.json` que escribe el SDK
/// (`rayito._templates._instructions.TEMPLATE_SPEC_VERSION` /
/// `templates/instructions.ts` `TEMPLATE_SPEC_VERSION`): mismo valor en los
/// tres lenguajes.
pub const TEMPLATE_SPEC_VERSION: &str = "rayito.template/1";

/// Ruta fija en la que `rayd` busca el `StartSpec` al arrancar
/// (`_dockerfile.py`/`dockerfile.ts` `TEMPLATE_JSON_IMAGE_PATH`).
pub const TEMPLATE_SPEC_PATH: &str = "/etc/rayito/template.json";

/// Cuánto y con qué cadencia sondear `ready_cmd` antes de darlo por
/// fallido; mismos campos que `ReadyPoll` del SDK.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct ReadyPoll {
    #[serde(rename = "interval_seconds")]
    pub interval_seconds: f64,
    #[serde(rename = "timeout_seconds")]
    pub timeout_seconds: f64,
}

impl ReadyPoll {
    #[must_use]
    pub fn timeout(&self) -> Duration {
        Duration::from_secs_f64(self.timeout_seconds.max(0.0))
    }

    #[must_use]
    pub fn interval(&self) -> Duration {
        Duration::from_secs_f64(self.interval_seconds.max(0.0))
    }
}

/// El `/etc/rayito/template.json` que `rayd` lee una sola vez, al arrancar
/// (mismo esquema que `rayito._templates._instructions.StartSpec` /
/// `templates/instructions.ts` `StartSpec`, salvo que aquí `envs` es un
/// mapa, no pares, porque este lado no necesita preservar el orden de
/// inserción del DSL).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StartSpec {
    pub version: String,
    pub start_cmd: String,
    #[serde(default)]
    pub ready_cmd: Option<String>,
    pub user: String,
    #[serde(default)]
    pub workdir: Option<String>,
    #[serde(default)]
    pub envs: BTreeMap<String, String>,
    #[serde(default)]
    pub ready_poll: Option<ReadyPoll>,
}

/// Lo que `ready_decision` concluye a partir de un sondeo de `ready_cmd`
/// (ver el aviso de alcance en el docstring del módulo sobre `Fail`/`Retry`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TemplateReadyDecision {
    /// `ready_cmd` ausente, o ya salió con 0: nada que esperar.
    Ok,
    /// `ready_cmd` sigue sin salir con 0 y el plazo no se ha agotado.
    Retry,
    /// `ready_cmd` sigue sin salir con 0 y el plazo ya se agotó.
    Fail,
}

/// Decide el estado de un `StartSpec` con `ready_cmd`, dado el código de
/// salida del último sondeo (`None` si todavía no se ha sondeado) y si el
/// plazo (`ReadyPoll.timeout_seconds` desde el primer sondeo) ya se agotó.
/// Puro: ni ejecuta el comando ni mide el tiempo, eso es
/// `shell_ready_probe` y su llamador.
#[must_use]
pub fn ready_decision(
    ready_cmd: Option<&str>,
    last_exit_code: Option<i32>,
    deadline_exceeded: bool,
) -> TemplateReadyDecision {
    if ready_cmd.is_none() {
        return TemplateReadyDecision::Ok;
    }
    match last_exit_code {
        Some(0) => TemplateReadyDecision::Ok,
        _ if deadline_exceeded => TemplateReadyDecision::Fail,
        _ => TemplateReadyDecision::Retry,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn poll(timeout_seconds: f64) -> ReadyPoll {
        ReadyPoll {
            interval_seconds: 0.5,
            timeout_seconds,
        }
    }

    #[test]
    fn no_ready_cmd_is_always_ok() {
        assert_eq!(ready_decision(None, None, false), TemplateReadyDecision::Ok);
        assert_eq!(ready_decision(None, Some(1), true), TemplateReadyDecision::Ok);
    }

    #[test]
    fn a_zero_exit_is_ok_regardless_of_the_deadline() {
        assert_eq!(
            ready_decision(Some("test -e /tmp/ready"), Some(0), true),
            TemplateReadyDecision::Ok
        );
    }

    #[test]
    fn a_nonzero_exit_retries_until_the_deadline_then_fails() {
        assert_eq!(
            ready_decision(Some("test -e /tmp/ready"), Some(1), false),
            TemplateReadyDecision::Retry
        );
        assert_eq!(
            ready_decision(Some("test -e /tmp/ready"), None, false),
            TemplateReadyDecision::Retry
        );
        assert_eq!(
            ready_decision(Some("test -e /tmp/ready"), Some(1), true),
            TemplateReadyDecision::Fail
        );
    }

    #[test]
    fn ready_poll_converts_seconds_to_duration() {
        let p = poll(60.0);
        assert_eq!(p.timeout(), Duration::from_secs(60));
        assert_eq!(p.interval(), Duration::from_millis(500));
    }

    #[test]
    fn start_spec_round_trips_through_json() {
        let spec = StartSpec {
            version: TEMPLATE_SPEC_VERSION.to_string(),
            start_cmd: "python -m http.server 8000".to_string(),
            ready_cmd: Some("test -e /tmp/ready".to_string()),
            user: "1000".to_string(),
            workdir: None,
            envs: BTreeMap::from([("MODE".to_string(), "prod".to_string())]),
            ready_poll: Some(poll(60.0)),
        };
        let json = serde_json::to_string(&spec).expect("serializable");
        let parsed: StartSpec = serde_json::from_str(&json).expect("deserializable");
        assert_eq!(parsed, spec);
    }

    #[test]
    fn start_spec_parses_the_sdks_actual_json_shape() {
        let json = r#"{
            "version": "rayito.template/1",
            "start_cmd": "python app.py",
            "ready_cmd": null,
            "user": "1000",
            "workdir": null,
            "envs": {},
            "ready_poll": null
        }"#;
        let parsed: StartSpec = serde_json::from_str(json).expect("deserializable");
        assert_eq!(parsed.start_cmd, "python app.py");
        assert_eq!(parsed.ready_cmd, None);
        assert_eq!(parsed.ready_poll, None);
    }
}
