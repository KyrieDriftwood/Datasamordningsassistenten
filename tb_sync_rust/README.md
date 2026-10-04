# `tb-sync`

Fristående, optimerad Rust-CLI för TB-synkronisering. Läser ODT-källan
skrivskyddat, genererar Markdown-speglingen och åtgärdsplanen vid innehållsändring.

Bygg en gång och kör sedan binären direkt:

```powershell
& "$env:USERPROFILE\.cargo\bin\cargo.exe" build --release --manifest-path tb_sync_rust\Cargo.toml
.\tb_sync_rust\target\release\tb-sync.exe --diff
```

`--diff` synkroniserar och skriver sektionsdiff plus åtgärdsplan till terminalen.
`--check` kontrollerar utan att skriva. `--watch` bevakar ODT-filen.
`--odt PATH` väljer en annan ODT-fil.

Kör tester med:

```powershell
& "$env:USERPROFILE\.cargo\bin\cargo.exe" test --manifest-path tb_sync_rust\Cargo.toml
```

Direkta Rust-beroenden: `zip` och `quick-xml`.
