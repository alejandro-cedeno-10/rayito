import { create } from "@bufbuild/protobuf";
import { describe, expect, test } from "vitest";
import vectors from "../../../../testdata/s3-mounts/mount-specs.json" with { type: "json" };
import { InvalidArgumentError, MountError, UnimplementedError } from "../../src/errors.js";
import { ConfigureRequestSchema, SectionCode } from "../../src/gen/rayito/v1/configure_pb.js";
import {
  S3MountPhase,
  S3MountStateSchema,
  S3MountsStatusSchema,
} from "../../src/gen/rayito/v1/s3_mounts_pb.js";
import { S3Mount } from "../../src/s3-mounts/domain.js";
import {
  checkMountsSettled,
  checkSectionResult,
  fromProtoStatus,
  planS3Mounts,
  S3MountsSection,
  toProto,
} from "../../src/s3-mounts/section.js";

/** Compartidos con `rayd-core` y el SDK de Python: los tres validadores leen
 * los mismos casos (ver su `description`). */
type VectorMount = (typeof vectors.cases)[number]["mounts"][number];

function planVector(mounts: readonly VectorMount[]): void {
  planS3Mounts(
    Object.fromEntries(
      mounts.map((raw) => [
        raw.path,
        new S3Mount({
          bucket: raw.bucket,
          prefix: raw.prefix,
          readOnly: raw.read_only,
          allowOverwrite: raw.allow_overwrite,
          allowDelete: raw.allow_delete,
        }),
      ]),
    ),
  );
}

describe("shared mount-spec vectors", () => {
  test.each(vectors.cases.map((entry) => [entry.name, entry] as const))("%s", (_name, entry) => {
    if (entry.sdk === "ok") {
      expect(() => planVector(entry.mounts)).not.toThrow();
    } else {
      expect(() => planVector(entry.mounts)).toThrow(InvalidArgumentError);
    }
  });
});

describe("S3Mount domain", () => {
  test("defaults are read-only and bucket root", () => {
    const mount = new S3Mount({ bucket: "team-data" });
    expect(mount.readOnly).toBe(true);
    expect(mount.prefix).toBe("");
    expect(mount.allowOverwrite).toBe(false);
    expect(mount.allowDelete).toBe(false);
  });

  test("write flags are accepted with readOnly: false", () => {
    const mount = new S3Mount({
      bucket: "team-data",
      readOnly: false,
      allowOverwrite: true,
      allowDelete: true,
    });
    expect(mount.allowOverwrite).toBe(true);
    expect(mount.allowDelete).toBe(true);
  });
});

describe("planS3Mounts", () => {
  test("undefined mounts is undefined", () => {
    expect(planS3Mounts(undefined)).toBeUndefined();
  });

  test("accepts a plain object literal, like volumes", () => {
    const section = planS3Mounts({ "/mnt/data": new S3Mount({ bucket: "team-data" }) });
    expect(section?.mounts.get("/mnt/data")?.bucket).toBe("team-data");
  });

  test("returns a section with the required flag and name", () => {
    const mounts = new Map([["/mnt/data", new S3Mount({ bucket: "team-data" })]]);
    const section = planS3Mounts(mounts);
    expect(section).toBeInstanceOf(S3MountsSection);
    expect(section?.section).toBe("s3_mounts");
    expect(section?.requiredFlag).toBe("s3Mounts");
  });
});

describe("proto translation", () => {
  test("toProto round-trips every field", () => {
    const mounts = new Map([
      ["/mnt/data", new S3Mount({ bucket: "team-data", prefix: "team7/" })],
      [
        "/mnt/out",
        new S3Mount({
          bucket: "team-data",
          prefix: "runs/",
          readOnly: false,
          allowOverwrite: true,
        }),
      ],
    ]);
    const wire = toProto(mounts);
    expect(wire.mounts).toHaveLength(2);
    const byPath = Object.fromEntries(wire.mounts.map((entry) => [entry.mountPath, entry]));
    expect(byPath["/mnt/data"]?.bucket).toBe("team-data");
    expect(byPath["/mnt/data"]?.readOnly).toBe(true);
    expect(byPath["/mnt/out"]?.readOnly).toBe(false);
    expect(byPath["/mnt/out"]?.allowOverwrite).toBe(true);
  });

  test("section.fill copies the proto config onto the request", () => {
    const mounts = new Map([["/mnt/data", new S3Mount({ bucket: "team-data" })]]);
    const section = planS3Mounts(mounts);
    const request = create(ConfigureRequestSchema, {});
    section?.fill(request);
    expect(request.s3Mounts?.mounts[0]?.bucket).toBe("team-data");
    expect(request.s3Mounts?.mounts[0]?.mountPath).toBe("/mnt/data");
  });

  test("fromProtoStatus maps every phase", () => {
    const status = create(S3MountsStatusSchema, {
      mounts: [
        create(S3MountStateSchema, { mountPath: "/mnt/a", phase: S3MountPhase.MOUNTED }),
        create(S3MountStateSchema, { mountPath: "/mnt/b", phase: S3MountPhase.PENDING }),
        create(S3MountStateSchema, {
          mountPath: "/mnt/c",
          phase: S3MountPhase.FAILED,
          errorClass: "iam_denied",
        }),
      ],
    });
    const result = fromProtoStatus(status);
    expect(result.get("/mnt/a")).toEqual({ state: "mounted", lastErrorClass: undefined });
    expect(result.get("/mnt/b")).toEqual({ state: "pending", lastErrorClass: undefined });
    expect(result.get("/mnt/c")).toEqual({ state: "failed", lastErrorClass: "iam_denied" });
  });
});

describe("checkSectionResult", () => {
  test.each([SectionCode.APPLIED, SectionCode.PENDING])("%i raises nothing", (code) => {
    expect(() => checkSectionResult(code, "")).not.toThrow();
  });

  test("UNSUPPORTED raises UnimplementedError naming the option", () => {
    try {
      checkSectionResult(SectionCode.UNSUPPORTED, "");
      expect.unreachable();
    } catch (error) {
      expect(error).toBeInstanceOf(UnimplementedError);
      expect((error as UnimplementedError).feature).toBe("mounts");
    }
  });

  test("FAILED raises MountError with the wire error class", () => {
    try {
      checkSectionResult(SectionCode.FAILED, "iam_denied");
      expect.unreachable();
    } catch (error) {
      expect(error).toBeInstanceOf(MountError);
      expect((error as MountError).code).toBe("iam_denied");
    }
  });

  test("an unknown error class falls back to the unknown sentinel", () => {
    try {
      checkSectionResult(SectionCode.INVALID, "");
      expect.unreachable();
    } catch (error) {
      expect((error as MountError).code).toBe("unknown");
    }
  });
});

describe("checkMountsSettled", () => {
  const wanted = new Map([["/mnt/data", new S3Mount({ bucket: "team-data" })]]);

  test("true once every wanted path is mounted", () => {
    expect(checkMountsSettled(new Map([["/mnt/data", { state: "mounted" }]]), wanted, false)).toBe(
      true,
    );
  });

  test("keeps waiting while pending or unreported", () => {
    expect(checkMountsSettled(new Map([["/mnt/data", { state: "pending" }]]), wanted, false)).toBe(
      false,
    );
    expect(checkMountsSettled(new Map(), wanted, false)).toBe(false);
  });

  test("a failed mount raises its own error class", () => {
    const states = new Map([
      ["/mnt/data", { state: "failed" as const, lastErrorClass: "iam_denied" }],
    ]);
    expect(() => checkMountsSettled(states, wanted, false)).toThrow(
      expect.objectContaining({ code: "iam_denied" }),
    );
  });

  test("still pending at the deadline is a timeout", () => {
    expect(() =>
      checkMountsSettled(new Map([["/mnt/data", { state: "pending" }]]), wanted, true),
    ).toThrow(expect.objectContaining({ code: "timeout" }));
  });
});
