# Prompt Optimization Changelog

| Epoch | Git SHA | Prompt Hypothesis | Pass Rate | Code Suggestions | Notes |
|-------|---------|-------------------|-----------|--------------------|-------|
| 1 | 8240090 | Re-add corrected forbidden-substitutions table: fix čnosťi→čnosti typo; add vášeň and princíp entries; rephrase tvar entry to avoid forma/tvar constraint conflict (remove blanket "NEVER use forma"); reviewer: add tie-break rule preferring APPROVED_WITH_NOTES when borderline | 88.0% (88/100, run 13) | `prechecks.py::_oov_stem`: add `elif w.endswith("en") and len(w) >= 5: stem = w[:-2]` to cover vášeň→vášne/vášni false rejections (normalized "vasen" stem doesn't prefix-match "vasne") | Run 13 regressed vs run 12 (91.8%→88.0%); root causes: čnosť typo in run-13 prompt (+5), vášeň missing from list (+6), forma/tvar conflict (+1); persistent stuck terms (viera 6→6, starostlivosť 6→6) likely burn iteration budget due to co-occurring failures |
