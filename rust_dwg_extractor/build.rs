use std::env;
use std::path::PathBuf;

fn main() {
    let manifest_dir = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap());
    let vendor_dir = manifest_dir.join("vendor").join("libredwg-win64");
    let include_dir = vendor_dir.join("include");
    let lib_dir = vendor_dir.join("lib");

    // Link against the MinGW import library for libredwg-0.dll.
    println!("cargo:rustc-link-search=native={}", lib_dir.display());
    println!("cargo:rustc-link-lib=dylib=redwg");

    println!("cargo:rerun-if-changed=wrapper.h");
    println!("cargo:rerun-if-changed=build.rs");

    let bindings = bindgen::Builder::default()
        .header("wrapper.h")
        .clang_arg(format!("-I{}", include_dir.display()))
        // Only expose the small surface we actually need to keep the
        // generated bindings manageable and fast to compile.
        .allowlist_function("dwg_read_file")
        .allowlist_function("dwg_free")
        .allowlist_function("dwg_ref_object")
        .allowlist_function("dwg_object_to_INSERT")
        .allowlist_function("dwg_object_to_ATTRIB")
        .allowlist_function("dwg_obj_block_header_get_name")
        .allowlist_function("dwg_dynapi_entity_utf8text")
        .allowlist_type("Dwg_Data")
        .allowlist_type("Dwg_Object")
        .allowlist_type("Dwg_Object_Ref")
        .allowlist_type("Dwg_Entity_INSERT")
        .allowlist_type("Dwg_Entity_ATTRIB")
        .allowlist_type("Dwg_AcDbMTextObjectEmbedded")
        .allowlist_type("Dwg_Object_BLOCK_HEADER")
        .allowlist_type("DWG_OBJECT_TYPE")
        .derive_default(true)
        .generate()
        .expect("Kunde inte generera bindgen-bindningar mot dwg.h/dwg_api.h");

    let out_path = PathBuf::from(env::var("OUT_DIR").unwrap());
    bindings
        .write_to_file(out_path.join("bindings.rs"))
        .expect("Kunde inte skriva bindings.rs");
}
