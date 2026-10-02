/**
 * `src/configure-base.ts`: la parte pura del ejecutor compartido de
 * `configureSections` que `Sandbox.create()` corre tras el primer `Health`
 * (capacidad, una única `Configure`, resultado por sección y la espera
 * acotada a las secciones `PENDING`). Espejo de `test_m15_configure_base.py`.
 */

import { create } from "@bufbuild/protobuf";
import { describe, expect, test } from "vitest";

import {
  type ConfigureSection,
  checkConfigureResponse,
  requireCapabilities,
  settleTimeoutMs,
  stillPending,
} from "../../src/configure-base.js";
import { MountError, UnimplementedError } from "../../src/errors.js";
import {
  ConfigSection,
  ConfigureResponseSchema,
  ConfigureStatusResponseSchema,
  SectionCode,
  SectionResultSchema,
} from "../../src/gen/rayito/v1/configure_pb.js";
import { AgentFeaturesSchema } from "../../src/gen/rayito/v1/features_pb.js";
import {
  S3MountPhase,
  S3MountStateSchema,
  S3MountsStatusSchema,
} from "../../src/gen/rayito/v1/s3_mounts_pb.js";
import { S3Mount } from "../../src/s3-mounts/domain.js";
import { MOUNT_SETTLE_TIMEOUT_MS, planS3Mounts } from "../../src/s3-mounts/section.js";

function section(): ConfigureSection {
  const planned = planS3Mounts({ "/mnt/data": new S3Mount({ bucket: "team-data" }) });
  if (planned === undefined) {
    throw new Error("planS3Mounts returned no section");
  }
  return planned;
}

function response(code: SectionCode) {
  return create(ConfigureResponseSchema, {
    results: [create(SectionResultSchema, { section: ConfigSection.S3_MOUNTS, code })],
  });
}

function status(phase: S3MountPhase, errorClass = "") {
  return create(ConfigureStatusResponseSchema, {
    s3Mounts: create(S3MountsStatusSchema, {
      mounts: [create(S3MountStateSchema, { mountPath: "/mnt/data", phase, errorClass })],
    }),
  });
}

describe("configure-base", () => {
  test("requireCapabilities names the section whose flag is off", () => {
    const features = create(AgentFeaturesSchema, { configure: true, s3Mounts: false });
    expect(() => requireCapabilities([section()], features)).toThrow(UnimplementedError);
    expect(() => requireCapabilities([], features)).not.toThrow();
  });

  test("checkConfigureResponse returns only the PENDING sections", () => {
    const entry = section();
    expect(checkConfigureResponse(response(SectionCode.PENDING), [entry])).toEqual([entry]);
    expect(checkConfigureResponse(response(SectionCode.APPLIED), [entry])).toEqual([]);
  });

  test("checkConfigureResponse raises the section's own error", () => {
    expect(() => checkConfigureResponse(response(SectionCode.UNSUPPORTED), [section()])).toThrow(
      UnimplementedError,
    );
  });

  test("settleTimeoutMs is the largest pending section's own bound", () => {
    expect(settleTimeoutMs([])).toBe(0);
    expect(settleTimeoutMs([section()])).toBe(MOUNT_SETTLE_TIMEOUT_MS);
  });

  test("stillPending drops settled sections and keeps unsettled ones", () => {
    const entry = section();
    expect(stillPending(status(S3MountPhase.MOUNTED), [entry], false)).toEqual([]);
    expect(stillPending(status(S3MountPhase.PENDING), [entry], false)).toEqual([entry]);
  });

  test("stillPending lets the section raise its own failure and timeout", () => {
    const entry = section();
    expect(() => stillPending(status(S3MountPhase.FAILED, "not_found"), [entry], false)).toThrow(
      MountError,
    );
    expect(() => stillPending(status(S3MountPhase.PENDING), [entry], true)).toThrow(
      expect.objectContaining({ code: "timeout" }),
    );
  });
});
