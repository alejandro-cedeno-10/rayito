use std::error::Error;
use std::path::PathBuf;

/// M15 (Rayito 0.6, foundations): drop-in registry. Every `.proto` under
/// `proto/rayito/v1/` compiles automatically (plus any vendored protos a
/// feature adds under `vendor/`, outside the buf module so `buf` ignores
/// them) — a feature adding its own `<name>.proto` needs no edit here.
const PROTO_GLOB: &str = "../../proto/rayito/v1/*.proto";
const VENDOR_GLOB: &str = "vendor/**/*.proto";

fn main() -> Result<(), Box<dyn Error>> {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let proto_root = manifest_dir.join("../../proto");
    let mut files = sorted_glob(&manifest_dir, PROTO_GLOB)?;
    files.extend(sorted_glob(&manifest_dir, VENDOR_GLOB)?);
    for file in &files {
        println!("cargo:rerun-if-changed={}", file.display());
    }
    println!("cargo:rerun-if-changed={}", proto_root.display());
    println!(
        "cargo:rerun-if-changed={}",
        manifest_dir.join("vendor").display()
    );
    let descriptors = protox::compile(&files, [&proto_root, &manifest_dir.join("vendor")])?;
    let out_dir = PathBuf::from(std::env::var("OUT_DIR")?);
    tonic_prost_build::configure()
        .build_server(true)
        .build_client(true)
        .build_transport(false)
        .file_descriptor_set_path(out_dir.join("rayito_descriptor.bin"))
        .compile_fds(descriptors)?;
    Ok(())
}

/// Deterministic build output: glob order is filesystem-dependent, so every
/// match is sorted before compiling.
fn sorted_glob(base: &std::path::Path, pattern: &str) -> Result<Vec<PathBuf>, Box<dyn Error>> {
    let mut paths: Vec<PathBuf> = glob::glob(base.join(pattern).to_str().ok_or("non-UTF-8 path")?)?
        .collect::<Result<Vec<_>, _>>()?;
    paths.sort();
    Ok(paths)
}
