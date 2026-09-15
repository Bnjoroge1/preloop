//! Required status checks stay wired to real check runs.
//!
//! GitHub matches required checks to completed check runs by exact display
//! name, and preloop names its check runs from the workflow job's display
//! `name:` (falling back to the job id). Neither side knows about the other,
//! so renaming a job's display name silently orphans the ruleset entry: the
//! requirement sits at "Expected" forever and every merge blocks with no
//! error explaining why. This test fails the build instead.
//!
//! Source of truth for the other side: repository ruleset `main`
//! (id 20594250). If you add or rename an entry there, update
//! `REQUIRED_CHECKS` here to match, and vice versa.

use std::collections::BTreeSet;
use std::path::PathBuf;

const REQUIRED_CHECKS: &[&str] = &[
    "rust-lint",
    "rust shard 1 of 4",
    "rust shard 2 of 4",
    "rust shard 3 of 4",
    "rust shard 4 of 4",
    "property-tests-fast",
    "Server light conformance",
    "Runner light conformance",
];

fn workspace_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap()
}

fn display_names_of(workflow: &preloop_gha_parser::Workflow) -> Vec<String> {
    // Static-matrix expansion is what produces display names for sharded
    // jobs; reusable callers without their callee YAML cannot expand here,
    // so fall back to the caller job's own display name in that case.
    match preloop_gha_parser::expand_jobs(workflow) {
        Ok(plans) => plans.into_iter().map(|plan| plan.name).collect(),
        Err(_) => workflow
            .jobs
            .iter()
            .map(|(id, job)| job.name.clone().unwrap_or_else(|| id.clone()))
            .collect(),
    }
}

#[test]
fn every_required_check_is_produced_by_a_workflow() {
    let root = workspace_root();
    let mut produced = BTreeSet::new();
    let mut workflows = 0u32;
    for entry in std::fs::read_dir(root.join(".github/workflows")).unwrap() {
        let path = entry.unwrap().path();
        if path.extension().and_then(|e| e.to_str()) != Some("yml") {
            continue;
        }
        let text = std::fs::read_to_string(&path).unwrap();
        let workflow: preloop_gha_parser::Workflow = match serde_yaml::from_str(&text) {
            Ok(workflow) => workflow,
            Err(_) => continue,
        };
        workflows += 1;
        produced.extend(display_names_of(&workflow));
    }
    assert!(
        workflows > 0,
        "expected workflow files under .github/workflows"
    );

    let missing: Vec<&&str> = REQUIRED_CHECKS
        .iter()
        .filter(|check| !produced.contains(**check))
        .collect();
    assert!(
        missing.is_empty(),
        "required status checks with no matching workflow job display name \
         (rename the job or update ruleset `main` id 20594250 to match): {missing:?}"
    );
}

#[test]
fn live_main_ruleset_matches_required_checks() {
    // Opt-in only: default `cargo test` must not hit the network. CI/ops can
    // set PRELOOP_LIVE_RULESET_CHECK=1 when intentionally reconciling the
    // live GitHub ruleset against REQUIRED_CHECKS.
    if std::env::var_os("PRELOOP_LIVE_RULESET_CHECK").is_none() {
        eprintln!("skipping live ruleset check (set PRELOOP_LIVE_RULESET_CHECK=1 to run)");
        return;
    }
    let api_url =
        std::env::var("GITHUB_API_URL").unwrap_or_else(|_| "https://api.github.com".to_owned());
    let url = format!("{api_url}/repos/preloopdev/preloop/rulesets/20594250");
    let body: serde_json::Value = reqwest::blocking::Client::new()
        .get(url)
        .header(reqwest::header::ACCEPT, "application/vnd.github+json")
        .header(reqwest::header::USER_AGENT, "preloop-required-checks-test")
        .send()
        .expect("requesting main ruleset")
        .error_for_status()
        .expect("main ruleset API response")
        .json()
        .expect("decoding main ruleset");
    let contexts: BTreeSet<String> = body
        .get("rules")
        .and_then(serde_json::Value::as_array)
        .and_then(|rules| {
            rules.iter().find_map(|rule| {
                if rule.get("type").and_then(serde_json::Value::as_str)
                    != Some("required_status_checks")
                {
                    return None;
                }
                rule.get("parameters")?
                    .get("required_status_checks")?
                    .as_array()
            })
        })
        .expect("main ruleset required_status_checks rule")
        .iter()
        .map(|check| {
            check
                .get("context")
                .and_then(serde_json::Value::as_str)
                .expect("required status check context")
                .to_owned()
        })
        .collect();
    let expected: BTreeSet<String> = REQUIRED_CHECKS
        .iter()
        .map(|check| (*check).to_owned())
        .collect();
    assert_eq!(
        contexts, expected,
        "live ruleset `main` id 20594250 differs from REQUIRED_CHECKS"
    );
}
