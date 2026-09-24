## 5. Results

### 5.1 Technological scope

Table 4 reports the OLS, robust OLS, Poisson and negative binomial estimates of technological scope side by side. In the linear benchmark the year trend is positive and significant (0.070, p < 0.01) but the model's explanatory power is low (R² = 0.033). The residuals reject homoskedasticity (Breusch–Pagan test with all regressors in the auxiliary regression, χ²(8) = 99.6, p < 0.001; Koenker's version relaxing normality, LM = 39.7, p < 0.001), and the residual-versus-fitted plot (Figure S1 in the Supplementary material) shows the diagonal striping and fan shape typical of count data: the spread of the residuals grows with the fitted value, and the residuals are bounded from below. Heteroskedasticity-robust (HC1) standard errors leave the point estimates unchanged and preserve the significance of the year, China and PCT coefficients, but the Japan coefficient loses significance (p = 0.04 to 0.19). Correcting the standard errors, however, does not resolve the functional-form problem that arises from fitting a linear model to a count outcome, so we move to count models. The negative binomial's dispersion parameter is clearly distinguishable from zero (α = 0.268, standard error 0.020; ln(α) = −1.316, standard error 0.076); the likelihood-ratio test against Poisson for α = 0 is a boundary test with χ̄²(01) = 596.3 (p < 0.001), and the Poisson's Pearson χ² per degree of freedom of 2.92 also indicates overdispersion. The Akaike information criterion (AIC) falls from 5,477 for the Poisson to 4,883 for the negative binomial. Under overdispersion the Poisson standard errors are underestimated, at 60–65% of the negative binomial's (for example 0.040 against 0.062 for China and 0.080 against 0.133 for Japan); the change in the Japan coefficient's significance from 1% under Poisson to 10% under the negative binomial results from this difference together with a modest fall in the coefficient from 0.280 to 0.239. Applying heteroskedasticity-robust standard errors to the Poisson yields values equal to or larger than the negative binomial's (0.063 for China, 0.190 for Japan), so the Poisson stars overstate precision. We therefore take the negative binomial as the main model.

**Table 4.** Determinants of technological scope by specification (N = 928; US office is the reference; standard errors in parentheses; exponentiated Poisson and negative binomial coefficients are incidence-rate ratios (IRRs); the OLS AIC rests on a normal likelihood and is not directly comparable with the Poisson and negative binomial AICs, and is shown for reference only; no significance stars are attached to the ln(α) row (the test of α = 0 is the likelihood-ratio test in the text); \* p < 0.10, \*\* p < 0.05, \*\*\* p < 0.01).

| | OLS | OLS (robust) | Poisson | Negative binomial |
|---|:---:|:---:|:---:|:---:|
| Filing year (yc) | 0.070\*\*\* (0.019) | 0.070\*\*\* (0.020) | 0.013\*\*\* (0.002) | 0.014\*\*\* (0.003) |
| China (CN) | −1.321\*\*\* (0.370) | −1.321\*\*\* (0.330) | −0.241\*\*\* (0.040) | −0.242\*\*\* (0.062) |
| Korea (KR) | 0.089 (0.570) | 0.089 (0.630) | 0.017 (0.059) | 0.017 (0.094) |
| Taiwan (TW) | 0.102 (0.510) | 0.102 (0.544) | 0.016 (0.051) | 0.021 (0.083) |
| Japan (JP) | 1.737\*\* (0.845) | 1.737 (1.328) | 0.280\*\*\* (0.080) | 0.239\* (0.133) |
| EPO (EP) | −0.032 (0.487) | −0.032 (0.453) | −0.005 (0.050) | −0.007 (0.080) |
| PCT (WO) | −1.071\*\* (0.432) | −1.071\*\*\* (0.399) | −0.193\*\*\* (0.047) | −0.191\*\*\* (0.073) |
| Other | 0.124 (1.029) | 0.124 (1.556) | 0.024 (0.106) | 0.056 (0.171) |
| Constant | 2.506\*\*\* (0.944) | 2.506\*\* (1.046) | 1.143\*\*\* (0.107) | 1.069\*\*\* (0.175) |
| ln(α) | | | | −1.316 (0.076) |
| R² | 0.033 | 0.033 | — | — |
| AIC | 5,224.9 | 5,224.9 | 5,477.2 | 4,883.0 |

Three patterns emerge. First, the full-period linear trend has an IRR of 1.015, so the number of subgroups grows by about 1.5% a year, but this trend is driven by the sparse early filings before 2010. In the post-2010 sample the sign reverses (IRR 0.970, p < 0.01): more recent applications are narrower (Section 5.4). We therefore refrain from a definite interpretation of the time trend in scope. Second, relative to the US reference, applications at the Chinese office are about 21% narrower (IRR 0.785, p < 0.01) and PCT applications about 17% narrower (IRR 0.826, p < 0.01), whereas applications at the Japanese office are the broadest in the sample (IRR 1.270, p < 0.10). Third, Korea (IRR 1.018), Taiwan (1.021) and the EPO (0.993) are indistinguishable from the US reference. Figure 3 plots the model-implied predicted scope by filing office. Because the Japan coefficient is significant only at the 10% level and not under heteroskedasticity-robust standard errors, the scope component of H3b receives only weak evidence (Section 5.4); the narrowness of Chinese-office applications supports the scope component of H3a and fits the catch-up interpretation.

![](figures/fig4_predicted_breadth_en.png)

**Figure 3.** Predicted technological scope by filing office (negative binomial; filing year fixed at the sample mean yc = 49.7 (2017.7); delta-method 95% confidence intervals).

### 5.2 Front-end orientation: binary logit

Table 5 reports the binary logit for front-end orientation (*has_process*). Technological scope is the strongest predictor: each additional CPC subgroup raises the odds that a patent claims fabrication-process technology by about 27% (OR 1.267, p < 0.01). The direction agrees with H1, but because front-end orientation includes process-only patents that do not cross the boundary, the main test of H1 is the multinomial logit of the next section. The year trend runs the other way (OR 0.946 per year, p < 0.01), which we interpret as a falling share of front-end-oriented filings as bonding- and assembly-level inventions became the mainstream of the field. Among offices only the Chinese office differs significantly from the US reference, with 65% higher odds of claiming front-end technology (OR 1.650, p < 0.05), which supports H3a. The odds ratio for the Japanese office is below one (0.483) but rests on 26 applications and is imprecisely estimated. The in-sample area under the ROC curve is 0.707, at the lower end of the "acceptable" range (0.7–0.8) of Hosmer, Lemeshow, and Sturdivant (2013) (Figure S2 in the Supplementary material).

**Table 5.** Binary logit of front-end orientation (has_process = 1 if any H01L21 symbol is present; office reference = US). Odds ratios; parentheses are delta-method standard errors of the odds ratios (significance from z tests on the log odds); \* p < 0.10, \*\* p < 0.05, \*\*\* p < 0.01.

| | Odds ratio | Standard error |
|---|:---:|:---:|
| Filing year (yc) | 0.946\*\*\* | (0.011) |
| Technological scope | 1.267\*\*\* | (0.034) |
| China (CN) | 1.650\*\* | (0.338) |
| Korea (KR) | 1.430 | (0.467) |
| Taiwan (TW) | 1.555 | (0.451) |
| Japan (JP) | 0.483 | (0.233) |
| EPO (EP) | 1.170 | (0.312) |
| PCT (WO) | 1.398 | (0.329) |
| Other | 2.132 | (1.307) |
| N | 928 | |
| Log-likelihood | −547.6 | |
| AIC | 1,115.1 | |
| AUC | 0.707 | |
