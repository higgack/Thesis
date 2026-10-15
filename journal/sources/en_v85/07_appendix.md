## Appendix

### Appendix A. Sample construction: Search strategy and irrelevant uses of the search terms

Table A1 presents the search strategy for patents related to hybrid bonding.

**Table A1.** Search strategy for patents related to hybrid bonding.

| Step | Condition | Content |
|------------------|-----------|--------------------------------------------|
| Step 1: CPC | H01L21 | Semiconductor fabrication-process technology, including direct wafer bonding (21/18, 21/20), device isolation, interconnects, and through-substrate vias (21/76), substrate and insulating-layer treatment (21/02), and chemical mechanical polishing (21/30–21/32); also includes packaging-stage fabrication processes (21/48, 21/50–21/58, 21/60, 21/78) and processing apparatus (21/67–21/68) |
| Step 1: CPC | H01L24 | Connector structures for connecting or disconnecting semiconductor or solid-state bodies, bump, layer, and wire connections, and related connecting methods and apparatus |
| Step 2: Keyword | Title | At least one of *hybrid bonding*, *direct bonding*, *Cu–Cu*, *copper to copper*, and *metal-oxide bond* |

*Note:* Step 1 requires at least one of the two classes, and Step 2 at least one of the keywords; both conditions must be met. The indexing codes of the two classes (e.g., H01L2224) were also used in the search, but because only the symbols of the classes proper were retained in the extracted records, indexing codes are not included in the calculation of technological scope.

The search terms of the keyword filter are also used in senses unrelated to hybrid bonding. Most such cases use *direct bonding* in a different sense, and a few use *hybrid bonding* in the sense of chemical bonding or bonding sheets for power modules. Applications whose titles contain the following terms were judged to be irrelevant uses: ceramic, DBC (direct bonded copper), alumina, zirconia, and aluminum nitride substrates; copper alloys; diamond–molybdenum bonding; laser precursors; electrostatic chucks; power module and power device substrates; heat spreaders; direct bonded metal substrates; direct bonded circuit assemblies; and sp3–sp2 hybrid bonds (in the sense of chemical bonding). These applications number 68 (63 for *direct bonding* and 5 for *hybrid bonding*), and their distribution is shown in Table A2. Many of the applications filed before 2010 that remain after the irrelevant uses are excluded concern direct bonding of silicon wafers (e.g., for manufacturing silicon-on-insulator wafers); they were retained in the sample because they are precursor technologies of the dielectric bonding that forms part of hybrid bonding. The list of the titles of the 68 applications is available from the authors upon request.

**Table A2.** Distribution of the 68 applications judged to be irrelevant uses.

| Category | Applications |
|---|---:|
| Type: bonding-only | 34 |
| Type: integrated | 17 |
| Type: process-only | 17 |
| Filing period: 1968–2014 | 36 |
| Filing period: 2015–2024 | 32 |

### Appendix B. Strongest links of the technology network

Table B1 lists the ten symbol pairs most frequently assigned together in the network of all CPC symbols assigned to the 928 applications. The network consists of CPC symbols as nodes and of undirected links between any two different symbols assigned to the same application, and the weight of a link is the number of applications to which both symbols are assigned. The strongest link, between the direct bonding of conductive surfaces and the direct bonding of insulating surfaces, directly reflects the principle of hybrid bonding, which bonds both the metal and the dielectric.

**Table B1.** Technology network: Symbol pairs most frequently assigned together (N = 928; top 10 pairs).

| Rank | Symbol pair | Content | Applications |
|------:|------------------------------------|----------------------|------:|
| 1 | 2224/80895 – 2224/80896 | Direct bonding of conductive surfaces – direct bonding of insulating surfaces | 460 |
| 2 | 24/08 – 24/80 | Bonding pad after connection – connection methods using bonding pads | 439 |
| 3 | 24/80 – 2224/80895 | Connection methods using bonding pads – direct bonding of conductive surfaces | 434 |
| 4 | 24/08 – 2224/80895 | Bonding pad after connection – direct bonding of conductive surfaces | 420 |
| 5 | 24/80 – 2224/80896 | Connection methods using bonding pads – direct bonding of insulating surfaces | 418 |
| 6 | 24/08 – 2224/80896 | Bonding pad after connection – direct bonding of insulating surfaces | 395 |
| 7 | 24/08 – 2224/08145 | Bonding pad after connection – bumpless bonding of stacked chips | 392 |
| 8 | 24/80 – 2224/08145 | Connection methods using bonding pads – bumpless bonding of stacked chips | 392 |
| 9 | 2224/08145 – 2224/80895 | Bumpless bonding of stacked chips – direct bonding of conductive surfaces | 379 |
| 10 | 2224/08145 – 2224/80896 | Bumpless bonding of stacked chips – direct bonding of insulating surfaces | 368 |

*Note:* The prefix H01L is omitted from the symbols. The application count for the second-ranked pair (24/08 – 24/80) is identical when recounted from the regression data.

### Appendix C. Full re-estimation results

Tables C1 and C2 present the full coefficients of the re-estimations summarized in Table 7 (except the clustered-standard-error row, whose point estimates equal the baseline), with the adjusted-scope specification and the 2010–2024 sample added. Each column is a multinomial logit in which one sample or variable definition is changed, and the baseline column is identical to the multinomial logit of Table 4. Filing office dummies are included as control variables in all columns, but their coefficients are omitted. Adjusted scope refers to the type-minimum-adjusted CPC scope (breadth_adj), which subtracts one symbol for each class an application carries; same-title deduplication to the sample that keeps only the earliest application among those with the same title; the narrow fabrication-process definition to the variable that excludes the packaging-stage process symbols of H01L21 from fabrication processes; irrelevant uses excluded to the sample without the 68 applications of Appendix A; and the *hybrid bonding* sample to the sample that keeps only applications whose titles contain *hybrid bonding*. The 2010–2024 sample is the sample of applications filed in 2010 or later mentioned in Section 5.2.

**Table C1.** Integrated-type equation of the multinomial logit: Relative risk ratios by sample and variable (base category = bonding-only; filing office dummies included; standard errors of the logit coefficients in parentheses; \* p < 0.10, \*\* p < 0.05, \*\*\* p < 0.01).

| | Baseline | Adjusted scope | Same-title deduplication | Narrow fabrication-process definition | Irrelevant uses excluded | *hybrid bonding* sample | 2010–2024 |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| Technological scope | 1.403\*\*\* (0.031) | 1.262\*\*\* (0.027) | 1.295\*\*\* (0.039) | 1.352\*\*\* (0.028) | 1.384\*\*\* (0.031) | 1.485\*\*\* (0.044) | 1.385\*\*\* (0.032) |
| Filing year (yc) | 0.993 (0.015) | 1.002 (0.014) | 0.997 (0.018) | 0.967\*\* (0.015) | 0.949\*\* (0.022) | 0.965 (0.033) | 0.934\*\* (0.027) |
| N | 928 | 928 | 473 | 908 | 860 | 467 | 830 |
| Log-likelihood | −774.5 | −829.4 | −425.3 | −755.9 | −682.7 | −364.0 | −683.0 |

**Table C2.** Process-only-type equation of the multinomial logit: Relative risk ratios by sample and variable (base category = bonding-only; filing office dummies included; standard errors of the logit coefficients in parentheses; \* p < 0.10, \*\* p < 0.05, \*\*\* p < 0.01).

| | Baseline | Adjusted scope | Same-title deduplication | Narrow fabrication-process definition | Irrelevant uses excluded | *hybrid bonding* sample | 2010–2024 |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| Technological scope | 0.905\*\* (0.048) | 0.915\*\* (0.044) | 0.826\*\*\* (0.061) | 0.923\* (0.045) | 0.916\* (0.050) | 1.129\* (0.062) | 0.910\* (0.051) |
| Filing year (yc) | 0.913\*\*\* (0.015) | 0.911\*\*\* (0.015) | 0.935\*\*\* (0.016) | 0.898\*\*\* (0.015) | 0.820\*\*\* (0.023) | 1.029 (0.057) | 0.862\*\*\* (0.033) |
| N | 928 | 928 | 473 | 908 | 860 | 467 | 830 |
| Log-likelihood | −774.5 | −829.4 | −425.3 | −755.9 | −682.7 | −364.0 | −683.0 |

**Alternative-exclusion sensitivity analysis.** To examine indirectly the sensitivity to the IIA assumption, a binary logit of integrated versus bonding-only was estimated on the sample excluding the process-only type (N = 763). The odds ratio of technological scope was 1.463 and that of filing year 0.987, similar in direction and relative magnitude to the values in the integrated-type equation of the multinomial logit (1.403 and 0.993). The substantive interpretation of the results for the integrated type can thus be regarded as largely unchanged when the process-only type is excluded, although this analysis does not replace a formal IIA test (Hausman–McFadden or Small–Hsiao). The core technology model in the main text also uses this sample.
