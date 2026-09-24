import { describe, expect, test } from "vitest";
import { CodeService } from "../../src/gen/rayito/v1/code_pb.js";
import { FilesystemService } from "../../src/gen/rayito/v1/filesystem_pb.js";
import { HealthService } from "../../src/gen/rayito/v1/health_pb.js";
import { LifecycleService } from "../../src/gen/rayito/v1/lifecycle_pb.js";
import { ProcessService } from "../../src/gen/rayito/v1/process_pb.js";
import { PtyServerMessageSchema, PtyService } from "../../src/gen/rayito/v1/pty_pb.js";

describe("generated surface (protoc-gen-es v2.15.0, target=ts)", () => {
  test("ProcessService.Start is a server stream that starts with StartEvent", () => {
    expect(ProcessService.method.start.methodKind).toBe("server_streaming");
    expect(ProcessService.method.connect.methodKind).toBe("server_streaming");
    expect(ProcessService.method.sendSignal.methodKind).toBe("unary");
  });

  test("FilesystemService.Write is a client stream", () => {
    expect(FilesystemService.method.write.methodKind).toBe("client_streaming");
    expect(FilesystemService.method.read.methodKind).toBe("server_streaming");
    expect(FilesystemService.method.watchDir.methodKind).toBe("server_streaming");
  });

  test("CodeService exposes Reattach and Execute streams", () => {
    expect(CodeService.method.reattach).toBeDefined();
    expect(CodeService.method.reattach.methodKind).toBe("server_streaming");
    expect(CodeService.method.execute.methodKind).toBe("server_streaming");
  });

  test("PtyServerMessage carries seq and the message oneof", () => {
    const fields = PtyServerMessageSchema.fields.map((field) => field.name);
    expect(fields).toContain("seq");
    expect(PtyServerMessageSchema.oneofs.map((oneof) => oneof.name)).toEqual(["message"]);
    expect(PtyService.method.create.methodKind).toBe("server_streaming");
    expect(PtyService.method.connect.methodKind).toBe("server_streaming");
  });

  test("HealthService methods are unary", () => {
    expect(HealthService.method.health.methodKind).toBe("unary");
    expect(HealthService.method.metrics.methodKind).toBe("unary");
  });

  test("LifecycleService.SetTimeout is unary", () => {
    expect(LifecycleService.method.setTimeout.methodKind).toBe("unary");
  });
});
