## ADDED Requirements

### Requirement: ADR-012 records the in-guest egress policy
`ARCHITECTURE.md` SHALL carry ADR-012 "Política de egress en el guest sobre rayito-base-caps; conectores de plataforma sólo por VPC del cliente".

The ADR SHALL contain:
- the context (Q44 and Q60: `[]` ≡ omitted, no `NO_EGRESS`; Q48: uidrange routing; no `UpdateMicrovm`);
- the decision (routes plus the local proxy, three modes, capability-gated, fail-closed default image);
- the consequences (guest-kernel residual, proxy-unaware clients fail closed, DNS not blocked on caps because the platform resolvers listen inside the guest, VPC connector as the only platform control);
- the addendum "Adenda: DNS bajo deny-all en caps" with context (QE1), decision (option C: names may resolve under deny-all, every connection outside the VM fails, hostname rules and proxy mode unchanged), consequences (the DNS-exfiltration residual channel), the alternatives considered (A: a port-53 `ip rule` before the `local` rule, deferred to the next cycle; B: a per-process `resolv.conf`, rejected) and reversibility.

The "Capa 2 — Agente rayd" services table SHALL list `NetworkService` (`UpdateNetwork`, `GetNetwork`). A subsection "Política de egress (M9)" SHALL give:
- the slot map (tables 101/102/103 at priorities 150/151/149 after `local` and the IMDS rule at 100);
- the mode table;
- the proxy protocols and guard;
- the environment export.

The hexagonal tables SHALL list the `network` domain module and the `egress_routes` and `network/` adapters. The image-variants paragraph SHALL say that `rayito-base-caps` also enforces the egress policy. A new `docs/site/docs/network.md` page SHALL be linked from the `mkdocs.yml` nav and SHALL list the divergences from E2B.

#### Scenario: ADR and page present
- **WHEN** a reviewer opens `ARCHITECTURE.md` and the docs site navigation
- **THEN** ADR-012 follows ADR-011 with context, decision, consequences and the DNS addendum, the services table lists `NetworkService`, the egress subsection shows the priority map, and the "Red saliente" page lists the proxy-unaware, ports 80/443, DNS, UDP, new-connections-only, lazily-started-proxy, root-not-filtered and caps-only divergences
