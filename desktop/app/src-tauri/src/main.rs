// Desktop entry point; Android starts the app through `run` in lib.rs.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    tradesetu_lib::run()
}
