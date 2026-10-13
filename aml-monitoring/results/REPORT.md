# AML monitoring - results

Config `2026.10-r4+c082bd1d7176` | seed 20261013 | 4,080 customers, 4,080 accounts, 235,439 transactions, 61-day monitoring window after a 31-day lookback.
Ground truth: 80 suspicious customers in 41 injected typology instances.

## Headline

| Metric | Value |
|---|---:|
| Typology instance detection (any alert) | 100.0% |
| Typology instance detection (above the line, score >= 40.0) | 87.8% |
| Alerts | 416 (102.0 per 1k customers) |
| Alert precision | 54.6% |
| False-positive rate (benign customers alerted) | 3.7% |
| Cases above / below the line | 75 / 114 |
| Case precision above the line | 49.3% |
| Cases above the line per 1k customers | 18.4 |
| SARs filed (simulated analysts) | 32 of 75 reviewed (42.7%) |
| Audit chain | 1096 entries, verified=True |

## Detection by typology

| Typology | Instances | Detected (alerts) | Detected (at line) | Members covered (at line) | Variants (instances / alerted / at line) |
|---|---:|---:|---:|---:|---|
| high_risk_layering | 8 | 100.0% | 87.5% | 87.5% | high_tier: 7/7/7; medium_only: 1/1/0 |
| mule_network | 4 | 100.0% | 100.0% | 100.0% | ring: 4/4/4 |
| rapid_movement | 12 | 100.0% | 91.7% | 91.7% | fast: 9/9/9; slow: 3/3/2 |
| round_tripping | 5 | 100.0% | 100.0% | 100.0% | domestic_loop: 4/4/4; offshore_loop: 1/1/1 |
| structuring | 12 | 100.0% | 75.0% | 75.0% | classic: 9/9/9; low_and_slow: 3/3/0 |

## Alerts by rule

| Rule | Alerts | True positive | Precision |
|---|---:|---:|---:|
| GEO-01 | 39 | 19 | 48.7% |
| GRA-COM | 46 | 46 | 100.0% |
| GRA-CYC | 30 | 30 | 100.0% |
| GRA-FIN | 18 | 18 | 100.0% |
| RMF-01 | 100 | 55 | 55.0% |
| STR-01 | 12 | 9 | 75.0% |
| VEL-01 | 171 | 50 | 29.2% |

## Benign look-alikes (where false positives come from)

| Segment | Customers | Alerted | In a case above the line |
|---|---:|---:|---:|
| cash_intensive_business | 181 | 2 | 0 |
| contract_payment_spike | 36 | 19 | 1 |
| family_support_wire | 19 | 19 | 0 |
| importer | 73 | 1 | 0 |
| intercompany_group | 30 | 0 | 0 |
| life_event | 105 | 105 | 37 |
| nonprofit_donations | 18 | 0 | 0 |
| property_management | 80 | 0 | 0 |
| remitter | 237 | 0 | 0 |
| staffing_payroll | 33 | 0 | 0 |
| title_escrow | 25 | 0 | 0 |

## Analyst simulation

Cases worked in priority-queue order by 4 L1 analysts, escalations to L2 and QA. Decisions follow ground truth with noise. SARs on true cases: 31; on benign cases: 1; true cases closed without SAR: 6; decision accuracy 90.7%.

## Above/below the line: case-score threshold sweep

| Line | Cases | Cases per 1k | Typology detection | Case precision |
|---:|---:|---:|---:|---:|
| 0 | 189 | 46.3 | 100.0% | 22.2% |
| 5 | 189 | 46.3 | 100.0% | 22.2% |
| 10 | 189 | 46.3 | 100.0% | 22.2% |
| 15 | 163 | 40.0 | 95.1% | 24.5% |
| 20 | 144 | 35.3 | 92.7% | 27.1% |
| 25 | 121 | 29.7 | 92.7% | 32.2% |
| 30 | 85 | 20.8 | 87.8% | 43.5% |
| 35 | 80 | 19.6 | 87.8% | 46.2% |
| 40 | 75 | 18.4 | 87.8% | 49.3% |
| 45 | 69 | 16.9 | 87.8% | 53.6% |
| 50 | 52 | 12.7 | 87.8% | 71.2% |
| 55 | 38 | 9.3 | 80.5% | 89.5% |
| 60 | 32 | 7.8 | 70.7% | 93.8% |
| 65 | 26 | 6.4 | 61.0% | 100.0% |
| 70 | 25 | 6.1 | 58.5% | 100.0% |
| 75 | 22 | 5.4 | 51.2% | 100.0% |
| 80 | 16 | 3.9 | 36.6% | 100.0% |
| 85 | 12 | 2.9 | 29.3% | 100.0% |
| 90 | 11 | 2.7 | 26.8% | 100.0% |
| 95 | 11 | 2.7 | 26.8% | 100.0% |

## Rule threshold sweeps (one rule at a time)

**STR-01.band_low_dollars** (target typology: structuring)

| Value | Alerts | True positive | Precision | Target detection |
|---:|---:|---:|---:|---:|
| 6000 | 45 | 10 | 22.2% | 83.3% |
| 7000 | 23 | 9 | 39.1% | 75.0% |
| 8000 | 12 | 9 | 75.0% | 75.0% |
| 9000 | 8 | 8 | 100.0% | 66.7% |

**STR-01.min_count** (target typology: structuring)

| Value | Alerts | True positive | Precision | Target detection |
|---:|---:|---:|---:|---:|
| 2 | 12 | 9 | 75.0% | 75.0% |
| 3 | 12 | 9 | 75.0% | 75.0% |
| 4 | 9 | 9 | 100.0% | 75.0% |
| 5 | 9 | 9 | 100.0% | 75.0% |

**RMF-01.out_window_hours** (target typology: rapid_movement)

| Value | Alerts | True positive | Precision | Target detection |
|---:|---:|---:|---:|---:|
| 24 | 45 | 29 | 64.4% | 66.7% |
| 48 | 100 | 55 | 55.0% | 75.0% |
| 72 | 145 | 71 | 49.0% | 100.0% |
| 96 | 164 | 77 | 46.9% | 100.0% |

**RMF-01.out_ratio** (target typology: rapid_movement)

| Value | Alerts | True positive | Precision | Target detection |
|---:|---:|---:|---:|---:|
| 0.6 | 110 | 58 | 52.7% | 75.0% |
| 0.7 | 106 | 57 | 53.8% | 75.0% |
| 0.8 | 100 | 55 | 55.0% | 75.0% |
| 0.9 | 83 | 47 | 56.6% | 58.3% |

**VEL-01.volume_multiple** (target typology: rapid_movement)

| Value | Alerts | True positive | Precision | Target detection |
|---:|---:|---:|---:|---:|
| 2.0 | 204 | 50 | 24.5% | 100.0% |
| 3.0 | 183 | 50 | 27.3% | 100.0% |
| 4.0 | 171 | 50 | 29.2% | 100.0% |
| 6.0 | 150 | 48 | 32.0% | 91.7% |
| 8.0 | 138 | 47 | 34.1% | 91.7% |

**GEO-01.high_risk_min_dollars** (target typology: high_risk_layering)

| Value | Alerts | True positive | Precision | Target detection |
|---:|---:|---:|---:|---:|
| 10000 | 39 | 19 | 48.7% | 87.5% |
| 20000 | 39 | 19 | 48.7% | 87.5% |
| 40000 | 31 | 19 | 61.3% | 87.5% |
| 80000 | 17 | 16 | 94.1% | 87.5% |

**GRA-FIN.fan_in_min** (target typology: mule_network)

| Value | Alerts | True positive | Precision | Target detection |
|---:|---:|---:|---:|---:|
| 3 | 47 | 32 | 68.1% | 100.0% |
| 4 | 33 | 31 | 93.9% | 100.0% |
| 5 | 18 | 18 | 100.0% | 100.0% |
| 6 | 12 | 12 | 100.0% | 100.0% |
| 8 | 1 | 1 | 100.0% | 25.0% |
