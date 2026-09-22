use std::error::Error;
use std::path::{Path, PathBuf};

const PROTO_FILES: [&str; 6] = ["common", "health", "process", "filesystem", "pty", "code"];

fn main() -> Result<(), Box<dyn Error>> {
    let proto_root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../proto");
    let files = proto_paths(&proto_root);
    for file in &files {
        println!("cargo:rerun-if-changed={}", file.display());
    }
    let descriptors = protox::compile(&files, [&proto_root])?;
    let out_dir = PathBuf::from(std::env::var("OUT_DIR")?);
    tonic_prost_build::configure()
        .build_server(true)
        .build_client(true)
        .build_transport(false)
        .file_descriptor_set_path(out_dir.join("rayito_descriptor.bin"))
        .compile_fds(descriptors)?;
    Ok(())
}

fn proto_paths(proto_root: &Path) -> Vec<PathBuf> {
    PROTO_FILES
        .iter()
        .map(|name| proto_root.join("rayito/v1").join(format!("{name}.proto")))
        .collect()
}
