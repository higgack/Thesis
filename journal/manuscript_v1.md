# When the back end reaches into the front end: Technological convergence and boundary-spanning patents in semiconductor hybrid bonding

**HyungKyu Lee** ^a,\*

^a Department of Management of Technology, [University], Seoul, Republic of Korea

\* Corresponding author. E-mail: [institutional e-mail]

*Manuscript prepared for submission to* Technovation *(Elsevier). Article type: Research paper. Word count (main text, excluding references, tables and appendices): approx. 8,600.*

---

## Highlights

- Hybrid bonding patents test technological convergence inside a value chain.
- 63.6% of 928 PATSTAT applications claim front-end (H01L21) alongside bonding scope.
- Each additional CPC subclass raises the odds of a front-end claim by about 27%.
- Pure front-end claiming declines over time; boundary-spanning claiming does not.
- Chinese-office filings are process-oriented; Japanese-office filings rarely are.

---

## Abstract

Research on technological convergence has concentrated on the blurring of boundaries *between* industries. This paper examines convergence *within* an industry, across the long-standing division between front-end fabrication and back-end assembly in semiconductor manufacturing. Hybrid bonding—the direct, bumpless joining of copper pads and surrounding dielectric that now enables high-bandwidth memory and 3D logic—is a back-end process whose performance depends on front-end capabilities such as chemical–mechanical planarisation and surface activation. Using 928 hybrid-bonding patent applications (5,277 application–CPC records) from EPO PATSTAT, we operationalise convergence at the level of the individual patent as *boundary spanning*: whether an application classified under bonding and interconnect (H01L24) also claims fabrication-process technology (H01L21). Nearly two-thirds of applications do so. Binary and multinomial logit estimates show that technological scope is the strongest correlate of boundary spanning (odds ratio 1.27 per additional CPC subclass), that pure front-end claiming has declined over time while integrated claiming has held its share, and that filing office matters: applications at the Chinese office are markedly more process-oriented, whereas those at the Japanese office almost never take a process-only form. The results extend convergence theory to the intra-industry, cross-stage case, connect the patent-level mechanism to the architecture of the semiconductor value chain, and offer a reproducible co-classification design for detecting boundary spanning in other process technologies.

**Keywords:** technological convergence; industry architecture; hybrid bonding; advanced packaging; patent scope; multinomial logit

---

## 1. Introduction

For half a century the semiconductor industry organised itself around a clear division of labour. Transistors were fabricated on wafers in the *front end*; finished wafers were diced, assembled and packaged in the *back end*. The two stages were separated technologically, organisationally and geographically: front-end fabrication concentrated in capital-intensive fabs run by integrated device manufacturers and, later, by foundries, while packaging and test were progressively outsourced to specialist assembly and test subcontractors (Macher and Mowery, 2004; Brown and Linden, 2009; Kapoor, 2013). Because the interface between the two stages was standardised, innovation in one stage rarely required competence in the other.

That separation is now eroding. As dimensional scaling has slowed and its cost has risen, the industry has looked for performance beyond the transistor—in how dies are partitioned, stacked and interconnected (Arden et al., 2010; Iyer, 2016; Khan et al., 2018). Advanced packaging has become a locus of competitive advantage, and the most demanding of its processes, *hybrid bonding*, joins copper pads and the surrounding oxide directly, without solder bumps, at interconnect pitches below one micron (Lau, 2021, 2022). What makes hybrid bonding a strategically interesting case is that its success turns on capabilities that historically belonged to the front end: nanometre-scale chemical–mechanical planarisation, surface activation and sub-micron alignment. A back-end process has, in effect, become dependent on front-end know-how. Leading foundries have responded by internalising advanced packaging platforms rather than handing them to subcontractors (Lau, 2022), and industry observers increasingly describe the boundary between fabrication and packaging as blurred (KnowMade/Yole Group, 2024).

This paper asks whether that blurring can be observed, measured and explained at the level of the individual invention. We draw on the literature on technological convergence—the process by which previously distinct technological or industrial domains overlap and recombine (Rosenberg, 1963; Kodama, 1992; Curran and Leker, 2011; Sick and Bröring, 2022)—and extend it in a direction that has received little attention. Almost all empirical work on convergence studies the meeting of *different industries*: information and communication technology with consumer electronics (Gambardella and Torrisi, 1998; Hacklin et al., 2009), food with pharmaceuticals (Bröring et al., 2006; Curran et al., 2010), nanotechnology with biotechnology (No and Park, 2010). Convergence *within* an industry, across the stages of its own value chain, has not been examined with the same tools, even though it is precisely this kind of convergence that reshapes industry architecture—who does what, and who captures value (Jacobides et al., 2006).

We treat hybrid bonding as a natural setting for such an examination. Using 928 hybrid-bonding patent applications extracted from EPO PATSTAT, we define a *boundary-spanning patent* as one that is classified under bonding and interconnect (CPC H01L24) and also claims fabrication-process technology (CPC H01L21). Co-classification of this kind is the standard patent-based signal of convergence (Curran and Leker, 2011; Preschitschek et al., 2013; Kwon et al., 2020); what is new is its application to a boundary that runs through a single industry rather than between two. We then ask three questions. How prevalent is boundary spanning, and has its form changed as the technology matured? Which attributes of a patent—its technological scope, its timing, the office at which it is filed—are associated with boundary spanning? And how do those patterns relate to the strategies of the firms and jurisdictions that populate the hybrid-bonding ecosystem?

Three findings stand out. First, boundary spanning is not marginal: 63.6% of applications carry at least one front-end symbol, and the modal strategy is an *integrated* claim that combines both stages. Second, technological scope is the dominant correlate. Each additional CPC subclass a patent spans raises the odds that it claims fabrication-process technology by roughly 27%, and a multinomial specification shows that broad patents move toward the integrated type while narrow patents specialise. Over time the share of *pure* front-end claims has fallen, but the share of integrated claims has held steady relative to assembly-level claims; convergence has proceeded by recomposition rather than by a simple rise in cross-boundary claiming. Third, filing office matters in ways that map onto industry architecture and catch-up. Applications at the Chinese office are narrower in scope and far more likely to be process-only; applications at the Japanese office are the broadest and essentially never process-only.

The paper contributes to the technology and innovation management literature in three ways. Conceptually, it extends convergence theory from the inter-industry to the intra-industry, cross-stage case, and connects it to the literature on vertical specialisation and industry architecture in semiconductors (Langlois and Steinmueller, 1999; Kapoor and Adner, 2012; Kapoor, 2013). Empirically, it shifts the unit of analysis from aggregate co-classification indices to the individual patent, so that the determinants of boundary spanning can be estimated with discrete-choice models and the strategic heterogeneity that aggregate indices average away can be recovered (cf. Cho and Kim, 2014). Methodologically, it offers a transparent, reproducible design—two CPC classes, one co-classification rule, standard count and discrete-choice estimators—that can be transferred to other process technologies where a downstream stage begins to absorb upstream capabilities.

The remainder of the paper is organised as follows. Section 2 reviews the theoretical background and develops hypotheses. Section 3 describes the hybrid-bonding ecosystem that forms the empirical context. Section 4 presents data and methods; Section 5 reports results; Section 6 discusses theoretical and managerial implications and limitations; Section 7 concludes.

## 2. Theoretical background and hypotheses

### 2.1 Technological convergence: concept, stages and drivers

The idea that technologies developed in one domain migrate into others has a long lineage. Rosenberg (1963) showed how the nineteenth-century machine-tool industry emerged as firms recognised that operations developed for one product—firearms, sewing machines, bicycles—could be applied to others, a process he called technological convergence. Kodama (1992) later described *technology fusion*, in which firms combine previously separate technical disciplines to create capabilities that neither possessed alone, and argued that it required a different kind of R&D organisation from breakthrough-oriented research. Subsequent work distinguished convergence of *technologies* from convergence of *markets* and *industries*. Gambardella and Torrisi (1998) found that convergence of the underlying technologies in electronics did not necessarily imply convergence of the firms' product markets; Athreye and Keeble (2000) traced how convergence in computing reshaped ownership and firm boundaries; and Fai and von Tunzelmann (2001) used patent data to examine whether the technological competences of large firms in different industries were converging.

Hacklin et al. (2009), drawing on the information and communication technology industry, proposed that convergence unfolds in stages—from knowledge convergence, through technological and applicational convergence, to industrial convergence—and that these stages are co-evolutionary rather than sequential. Curran and Leker (2011) formalised the distinction between science, technology, market and industry convergence and showed how each could be monitored with patent and publication indicators. The literature has since matured into a recognised research stream in technology and innovation management, reviewed by Sick and Bröring (2022), with taxonomies of convergence patterns (Geum et al., 2016), frameworks for assessing its extent in high-technology environments (Sick et al., 2019) and analyses of the strategic choices firms face when their industries converge (Hacklin et al., 2013). Firm-level evidence on drivers is more recent: Hwang (2020), using panel data on Korean ICT firms, finds that collaboration among ICT firms is the collaboration type most strongly associated with convergence, while Caviggioli (2016) identifies the characteristics of technology fields that make fusion more likely.

Two features of this literature matter for the present study. The first is its focus on the meeting of *different* domains. The paradigmatic cases—telecommunications and computing, food and pharmaceuticals, nanotechnology and biotechnology—involve firms from separate industries discovering that their knowledge bases overlap. The second is the level at which convergence is typically measured: aggregate indicators of co-classification, co-citation or semantic similarity between two technology fields (Curran et al., 2010; Karvonen and Kässi, 2013; Preschitschek et al., 2013; Cho and Kim, 2014; Song et al., 2017). These indicators are well suited to detecting *whether* and *when* fields converge. They are less suited to explaining *which* inventions carry convergence and *why*, because they average over the strategic heterogeneity of individual patents.

### 2.2 Convergence across stages of a value chain: the semiconductor case

A different boundary is the one that separates stages of a single industry's value chain. The semiconductor industry provides an unusually clear example because its stages have been the subject of sustained research on vertical specialisation and industry architecture. Langlois and Steinmueller (1999) trace how competitive advantage shifted across firms and countries as the industry's structure evolved; Macher and Mowery (2004) document the vertical specialisation that separated design from fabrication and fabrication from assembly, giving rise to fabless firms, foundries and assembly-and-test subcontractors; Brown and Linden (2009) describe the successive crises through which this architecture was renegotiated. Kapoor (2013) shows that integration persisted alongside specialisation: firms that retained capabilities across stages navigated technological transitions differently from specialists, and their choices shaped the architecture of the industry. Kapoor and Adner (2012), studying semiconductor (DRAM) producers across successive technology generations, distinguish what firms *make* from what they *know*, and find that knowledge boundaries wider than production boundaries confer advantage when components and systems are interdependent—an argument that echoes Brusoni et al.'s (2001) observation that firms "know more than they make" precisely when technological interdependence across stages is high. Adner and Kapoor (2010) add that the location of innovation challenges within an ecosystem—upstream in components or downstream in complements—conditions who benefits from a new technology generation.

The relevance of this literature to convergence becomes clear once one recognises that vertical specialisation rests on a *stable interface* between stages. D. Ernst (2005) shows that the modularity on which vertical specialisation in chip design rests has limits as complexity rises; Jacobides et al. (2006) argue that industry architectures—the templates that define who does what and how value is divided—are themselves objects of competition, and that innovations which change the interdependence between stages invite their renegotiation. Hybrid bonding is such an innovation. Because its yield depends on wafer-level planarisation, surface chemistry and alignment, capabilities that reside in the front end, the interface between fabrication and packaging is no longer stable. The consequence is convergence not between two industries but between two stages of one industry: firms in the back end must acquire front-end competences, and firms in the front end find that their competences extend naturally into packaging. This is the phenomenon that the end-of-Moore's-law literature has anticipated in general terms (Arden et al., 2010; Iyer, 2016; Khan et al., 2018) and that the packaging engineering literature describes technically (Lau, 2021, 2022). What has been missing is a way to observe it in the record of invention.

### 2.3 Measuring convergence with patents: from field-level indices to the boundary-spanning patent

Patents are the most widely used data for measuring convergence because classification codes place each invention in one or more technology fields, and citations record knowledge flows between them (Curran and Leker, 2011). Three measurement traditions can be distinguished. The first builds *field-level indices* from co-classification: Cho and Kim (2014) apply entropy and gravity concepts to IPC co-occurrence and citation networks in printed electronics; Lee et al. (2015) predict convergence patterns from large-scale triadic patents; Jeong et al. (2015) locate convergence within a developmental-stage framework; Kwon et al. (2020) anticipate technology-driven industry convergence from large-scale patent analysis. The second uses *text and representation learning* to detect convergence before classification catches up: Preschitschek et al. (2013) compare semantic analysis with IPC co-classification, and Zhu and Motohashi (2022) train graph convolutional networks on patent-text keyword vectors. The third examines the *drivers* of convergence at the level of fields or firms (Caviggioli, 2016; Hwang, 2020).

The present study belongs to the first tradition in its choice of signal—co-classification—but departs from it in the unit of analysis. Rather than aggregating co-classification into a field-level index, we define a binary attribute of each patent: whether an invention classified in the back-end bonding class (H01L24) also carries a front-end fabrication symbol (H01L21). We call such a patent *boundary-spanning*. The attribute is directly interpretable as convergence at the level of the invention, it can be modelled with standard discrete-choice estimators, and it retains the heterogeneity across patents that field-level indices average away.

Two further strands of patent research inform the explanatory variables. The first concerns *patent scope*. Lerner (1994) measured the scope of a patent by the number of classification classes it spans and showed that scope has economic value; Marco et al. (2019) refine the measurement of scope through claim characteristics. In semiconductors specifically, Hall and Ziedonis (2001) documented the surge in patenting after the mid-1980s and Ziedonis (2004) showed that firms build larger portfolios where technology markets are fragmented—conditions under which broad claiming is a rational strategy. The second concerns *jurisdictional differences* in patenting behaviour. Filings at different patent offices reflect different applicant populations, examination practices and strategic purposes. Chinese patent statistics are shaped by subsidy programmes and catch-up dynamics that affect the quality and breadth of filings (Hu and Jefferson, 2009; Dang and Motohashi, 2015), and China's position in the semiconductor value chain has been evolving rapidly under geopolitical pressure (Grimes and Du, 2022). Japanese claiming practice changed after the 1988 reform that permitted multi-claim patents (Sakakibara and Branstetter, 2001), and Japanese firms have long been strong in semiconductor equipment and materials (Langlois and Steinmueller, 1999). Catch-up theory more generally predicts that latecomers concentrate on internalising specific production steps before claiming whole systems (Lee and Lim, 2001).

### 2.4 Hypotheses

The arguments above yield four expectations about which hybrid-bonding patents span the front-end/back-end boundary.

*Technological scope.* Kodama's (1992) fusion and Rosenberg's (1963) convergence both describe inventions that combine knowledge from more than one domain. If boundary spanning is a form of fusion, then patents that span more technology classes should be more likely to include a front-end class among them. The mechanism is partly mechanical—more classes mean more chances that one is H01L21—but it is also strategic: a patent that claims a full process flow from wafer preparation to bonded stack necessarily reaches into fabrication, whereas a patent that claims only an assembly step or a bonding structure does not (Lerner, 1994; Ziedonis, 2004).

> **H1.** The broader a hybrid-bonding patent's technological scope, the more likely it is to span the front-end/back-end boundary, i.e. to claim fabrication-process (H01L21) technology in addition to bonding (H01L24) technology.

*Maturation.* The stage models of convergence (Hacklin et al., 2009; Jeong et al., 2015) imply that the *form* of convergence changes as a technology matures. Early inventions in hybrid bonding concerned the fabrication steps that made direct bonding possible—surface preparation, low-temperature anneal, planarisation—and were naturally classified as process inventions. As the technology moved from wafer-to-wafer use in image sensors and 3D NAND toward die-to-wafer integration for high-bandwidth memory and logic, invention shifted toward the bonded structure and the integrated stack (Lau, 2021). We therefore expect the share of *pure* front-end (process-only) claims to decline over time relative to assembly-level claims, while boundary-spanning (integrated) claims persist.

> **H2.** As hybrid bonding matures, the probability that a patent takes a process-only form declines relative to assembly-level claiming, whereas the probability of an integrated (boundary-spanning) form does not decline.

*Jurisdiction and industry architecture.* Where a patent is filed reflects who is inventing and for what purpose. Catch-up theory (Lee and Lim, 2001) and the evidence on Chinese patenting (Hu and Jefferson, 2009; Dang and Motohashi, 2015; Grimes and Du, 2022) suggest that filings at the Chinese office will concentrate on internalising specific fabrication steps—narrower in scope and more often process-only. Conversely, the strength of Japanese firms in equipment and materials (Langlois and Steinmueller, 1999) and the multi-claim practice that followed the 1988 reform (Sakakibara and Branstetter, 2001) suggest that filings at the Japanese office will be broad and integration-oriented, and rarely process-only.

> **H3a.** Applications filed at the Chinese office are more likely than those filed at the U.S. office to be process-oriented (to claim H01L21) and, in particular, to take a process-only form.

> **H3b.** Applications filed at the Japanese office are broader in scope than those filed at the U.S. office and are less likely to take a process-only form.

These hypotheses concern associations between patent attributes and boundary spanning, not causal effects; we return to this distinction in Section 6.3.

## 3. Research context: the hybrid-bonding ecosystem

### 3.1 Technology

Hybrid bonding creates a permanent interconnect by bringing two planarised surfaces—each patterned with copper pads embedded in dielectric—into direct contact, so that dielectric bonds to dielectric at room temperature and copper bonds to copper after a low-temperature anneal. Because there are no solder bumps, the pitch of the interconnect can fall below one micron, an order of magnitude finer than micro-bump flip-chip, and the bonded interface is thin enough to be treated almost as a continuation of the back-end-of-line wiring (Lau, 2021, 2022). The process exists in two forms. Wafer-to-wafer (W2W) bonding joins whole wafers and was commercialised first in CMOS image sensors and 3D NAND; die-to-wafer (D2W) bonding attaches individual dies to a wafer and is the form required for heterogeneous integration of chiplets, high-bandwidth memory stacks and 3D logic. In both forms, the steps that determine yield—chemical–mechanical planarisation to sub-nanometre roughness, plasma or chemical surface activation, particle control and sub-micron alignment—are front-end competences in the CPC sense: they fall under H01L21, the class for processes and apparatus for manufacturing semiconductor devices, rather than under H01L24, the class for bonding and interconnect (Appendix A).

### 3.2 Filing dynamics and ecosystem structure

Two descriptive analyses of the same PATSTAT extraction, reported in full in the Supplementary Material (S1), set the scene. Fig. 1 compares annual filings for hybrid bonding, a *process* technology, with filings for high-bandwidth memory (HBM), the *product* that most visibly depends on it. The two series moved together until the late 2010s; they were roughly equal in 2019 (56 versus 41 filings) and began to separate in 2020 (71 versus 45). In 2021 hybrid-bonding filings jumped to 118, a year-on-year increase of about 66%, while HBM filings stagnated near 30. The technology's development thus ran ahead of the product's, as one would expect of an enabling process.

The applicant structure is concentrated. In the applicant-level analysis the ten largest applicants account for 58.5% of the ranked patents (Fig. 2), led by Intel and Adeia—the licensing company that absorbed Ziptronix and Invensas, the originators of direct-bond interconnect—followed by TSMC and the French research institute CEA. Independent industry analysis of a larger set of hybrid-bonding patents identifies TSMC, Adeia, YMTC, Intel and Samsung as the leading assignees (KnowMade/Yole Group, 2024), consistent with the ordering observed here. The presence of power-semiconductor and defence firms among smaller applicants indicates that the technology's applications extend beyond logic and memory.

Citation-based indicators, computed following Narin et al. (1987, 1997) and H. Ernst (2003), separate three strategic types (Table 1). The *originators*—Ziptronix and its principal inventor—hold patents cited on average more than 270 times, none of them uncited, with the highest science-linkage scores in the sample and the widest market coverage; they built the entry barrier that the rest of the ecosystem licenses or engineers around. The *leading manufacturer*, TSMC, combines a high citation impact with a very low science-linkage score, the profile of engineering-led process development; its revealed technological advantage (Soete, 1987) is concentrated in interconnect, via filling, damascene and planarisation codes—that is, in H01L21 front-end technology brought to bear on packaging, the pattern its SoIC platform embodies. The *catch-up entrant*, YMTC, licensed Adeia's foundational technology and then developed its own W2W "X-Stacking" NAND process; its 26 patents are all cited, its citations peak only two years after filing, and its science-linkage ratio is among the highest in the sample. Intel, by contrast, holds the largest number of patents (144) but has the lowest average citation impact among the major applicants, and Samsung's uncited share exceeds a third. Fig. 3 summarises the ecosystem as a technology network that maps these actors, the accompanying processes hybrid bonding requires (through-silicon vias, planarisation, dicing, surface preparation), and the structures and products it enables.

**Table 1.** Selected ecosystem indicators for major applicants (source: applicant-level analysis of the same PATSTAT extraction; Supplementary Material S1).

| Applicant (type) | Citations per patent | Uncited share | Science linkage | Market coverage (average market size / global patent ratio) | Strategic reading |
|---|---:|---:|---:|---|---|
| Ziptronix / Tong Qin-Yi (originator, IP) | 272.5 / 281.4 | 0% / 0% | 11.08 / 7.91 | 18.75 / 99.2%; 19.17 / 95.5% | Foundational patents; entry barrier |
| Invensas → Adeia (IP licensor) | — | — | 27.79 / 10.49 | — | Science-based licensing platform |
| TSMC (foundry) | 71.1 | 3.6% | 0.79 | 5.25 / 92.6% | Engineering-led; front-end codes internalised in packaging (SoIC) |
| YMTC (memory, catch-up) | 49.2 | 0% (26 patents) | 48.5% papers-cited ratio | 11.69 / 99.7% | Licensed then self-developed W2W NAND; citation peak at year 2 |
| Intel (IDM) | 4.6 | 22.8% (144 patents) | 0.33 | — | Largest portfolio, lowest impact |
| Samsung (memory/foundry) | 24.8 | 37.3% (11 patents) | — | 2.61 / 45.1% | Quantity without citation impact |
| Micron (memory) | — | — | 0.03 | 3.88 / 85.9% | Engineering-led |
| Applied Materials / Tokyo Electron (equipment) | — | — | 0.08 / — | — | Surface preparation and cluster tools; alignment metrology |

*Note:* Science linkage is the average number of non-patent literature references per patent; for YMTC the available statistic is the share of patents citing scientific papers. "—" indicates the statistic was not reported for that applicant in the source analysis.

This ecosystem-level picture motivates the patent-level analysis. It shows a leading foundry whose packaging patents rest on front-end codes, an originator whose science-based patents everyone must design around, and a catch-up entrant that moved quickly from licensing to independent process development. Whether these strategies leave a systematic imprint on *which patents* span the front-end/back-end boundary is the question the rest of the paper addresses.

## 4. Data and methods

### 4.1 Data

The data come from the European Patent Office's PATSTAT Global database, 2025 edition (European Patent Office, 2025). Candidate records were retrieved by CPC classification—H01L21 (processes and apparatus for manufacturing semiconductor devices) and H01L24 (arrangements for connecting or disconnecting semiconductor bodies), together with their indexing codes—and then filtered with an AND-combination of process keywords (*hybrid bonding*, *direct bonding*, *Cu–Cu*, *copper to copper*, *metal-oxide bond*) to remove false positives (Appendix A, Fig. A1). The extraction yields 5,277 application–CPC records, each pairing one application with one CPC symbol. Collapsing to the application level gives 928 unique applications filed between 1968 and 2024. Every symbol lies under H01L (semiconductor devices), and within it the records divide between H01L21 and H01L24, the two classes that define the front-end/back-end boundary for our purposes.

### 4.2 Variables

Table 2 defines the variables. *Breadth* is the number of distinct CPC subclass symbols on the application, our measure of technological scope in the tradition of Lerner (1994). *Has_process* is a binary indicator equal to one if the application carries any H01L21 symbol—our measure of boundary spanning. *Tech_cat* assigns each application to one of three mutually exclusive strategy types: *assembly-only* (H01L24 symbols only), *integrated* (both H01L24 and H01L21) and *process-only* (H01L21 only). *Ccode* is the office at which the application was filed, with eight levels (US, CN, KR, TW, JP, EP, WO, Other) and the U.S. office as reference. *Yc* is the filing year measured as years since the first filing in the sample.

**Table 2.** Variable definitions.

| Variable | Definition | Type |
|---|---|---|
| breadth | Number of distinct CPC subclass symbols on the application (technological scope) | Count, 1–21 |
| has_process | 1 if the application carries any H01L21 (fabrication-process) symbol; 0 otherwise | Binary |
| tech_cat | Assembly-only (H01L24 only) / Integrated (both) / Process-only (H01L21 only) | Categorical, 3 |
| ccode | Filing office: US (reference), CN, KR, TW, JP, EP, WO, Other | Categorical, 8 |
| yc | Filing year, years since the first filing in the sample | Continuous |

Breadth averages 5.69 subclasses (s.d. 4.07; range 1–21). The U.S. office accounts for 382 applications, followed by China (175), the PCT route (113), the EPO (83), Taiwan (75), Korea (58), Japan (26) and other offices (16). Fig. 4 shows the filing-year profile: negligible activity before 2010, a steep rise through the late 2010s, a peak around 2022, and a fall in 2023–2024 that reflects the lag before recent applications are published rather than a real decline. By strategy type, 425 applications (45.8%) are integrated, 338 (36.4%) assembly-only and 165 (17.8%) process-only; in total 590 applications, or 63.6%, carry at least one fabrication-process symbol.

One caveat governs the interpretation of every jurisdictional result. *Ccode* is the office at which an application was filed, not the nationality of the applicant, and WO and EP are international and regional routes rather than countries. Differences across offices therefore reflect filing and protection strategy as much as the origin of the invention, and we read them in that light throughout.

### 4.3 Empirical strategy

The dependent variables differ in statistical nature, and the models follow the variables. Because *breadth* is a count, we begin with ordinary least squares as a benchmark, test its residuals for heteroskedasticity (Breusch–Pagan) and inspect the residual-versus-fitted plot, then estimate Poisson and negative binomial models and test for overdispersion through the negative binomial's dispersion parameter (Hausman et al., 1984; Cameron and Trivedi, 2013). Coefficients in the preferred count model are reported as incidence-rate ratios. The breadth models serve two purposes: they characterise scope, which enters the boundary-spanning models as a regressor, and they test the scope component of H3b.

Boundary spanning is modelled as discrete choice. A binary logit predicts *has_process* from breadth, the year trend and filing office, reported as odds ratios and assessed by the area under the receiver operating characteristic curve (Hosmer et al., 2013). A multinomial logit (McFadden, 1974) then models *tech_cat* with assembly-only as the base category, reported as relative-risk ratios, so that the integrated and process-only branches can be read against the same reference. The multinomial logit relies on the independence of irrelevant alternatives; we interpret its estimates as relative tendencies against the base category and refrain from causal statements about absolute probabilities. Robust standard errors were computed alongside the count models; they changed the estimates little, indicating that the issue with the linear benchmark is functional form rather than the error structure.

## 5. Results

### 5.1 Technological scope

Table 3 summarises the breadth models; the full comparison of OLS, robust OLS, Poisson and negative binomial estimates is given in Appendix B (Table B1). In the linear benchmark the year trend is positive and significant (0.070, p < 0.01) but the model explains little variance (R² = 0.033). Its residuals reject constant variance (Breusch–Pagan p < 0.001) and display the banding and funnel shape characteristic of a count outcome. The negative binomial's dispersion parameter is large and precisely estimated (ln α = −1.316, α ≈ 0.27, p < 0.001), the likelihood-ratio test against Poisson is decisive, and the Akaike information criterion falls from 5,477 under Poisson to 4,883 under the negative binomial, which we therefore treat as the working model.

**Table 3.** Determinants of technological breadth: negative binomial estimates (N = 928; U.S. office is the reference; standard errors in parentheses; \* p < 0.10, \*\* p < 0.05, \*\*\* p < 0.01).

| | Coefficient | IRR |
|---|:---:|:---:|
| Filing year (yc) | 0.014\*\*\* (0.003) | 1.014 |
| China (CN) | −0.242\*\*\* (0.062) | 0.785 |
| Korea (KR) | 0.017 (0.094) | 1.017 |
| Taiwan (TW) | 0.021 (0.083) | 1.021 |
| Japan (JP) | 0.239\* (0.133) | 1.270 |
| EPO (EP) | −0.007 (0.080) | 0.993 |
| PCT (WO) | −0.191\*\*\* (0.073) | 0.826 |
| Other | 0.056 (0.171) | 1.058 |
| Constant | 1.069\*\*\* (0.175) | |
| ln(α) | −1.316\*\*\* (0.076) | |
| AIC | 4,883.0 | |

Three patterns emerge. Scope has widened as the field matured: the incidence-rate ratio on the year trend is 1.014, or about 1.4% more subclasses per year. Against the U.S. baseline, applications at the Chinese office are about 21% narrower (IRR 0.785, p < 0.01) and PCT applications about 17% narrower (IRR 0.826, p < 0.01), whereas applications at the Japanese office are the broadest in the sample (IRR ≈ 1.27, p < 0.10). Korea, Taiwan and the EPO do not separate from the U.S. baseline. Fig. 5 plots the predicted breadth by office implied by the model. The scope component of H3b—that Japanese-office filings are broader—is supported at the 10% level; the narrowness of Chinese-office filings is consistent with the catch-up reading advanced for H3a.

### 5.2 Boundary spanning: binary logit

Table 4 reports the binary logit for *has_process*. Breadth is the dominant predictor: each additional CPC subclass raises the odds that a patent claims fabrication-process technology by about 27% (OR 1.267, p < 0.01), supporting H1. The year trend runs the other way (OR 0.946 per year, p < 0.01): the *share* of process-oriented filings has fallen as bonding- and assembly-level invention has come to dominate the field. Among filing offices only the Chinese office differs significantly from the U.S. baseline, with 65% higher odds of a process claim (OR 1.650, p < 0.05), supporting H3a. The Japanese-office odds ratio is below one (0.483) but imprecisely estimated on 26 applications. The model discriminates acceptably, with an area under the ROC curve of 0.707 (Fig. 6).

**Table 4.** Binary logit of boundary spanning (has_process = 1 if any H01L21 symbol). Odds ratios; standard errors in parentheses; \* p < 0.10, \*\* p < 0.05, \*\*\* p < 0.01.

| | Odds ratio | (SE) |
|---|:---:|:---:|
| Filing year (yc) | 0.946\*\*\* | (0.011) |
| Breadth | 1.267\*\*\* | (0.034) |
| China (CN) | 1.650\*\* | (0.338) |
| Korea (KR) | 1.430 | (0.467) |
| Taiwan (TW) | 1.555 | (0.451) |
| Japan (JP) | 0.483 | (0.233) |
| EPO (EP) | 1.170 | (0.312) |
| PCT (WO) | 1.398 | (0.329) |
| Other | 2.132 | (1.307) |
| N = 928; log-likelihood = −547.6; AIC = 1,115.1; AUC = 0.707 | | |

### 5.3 Strategy types: multinomial logit

The binary model cannot distinguish the two ways a patent can avoid boundary spanning—by staying entirely in assembly or by staying entirely in fabrication. The multinomial logit in Table 5 separates them. Read against assembly-only, breadth pushes a patent toward the integrated type (RRR 1.403, p < 0.01) and away from the process-only type (RRR 0.905, p < 0.05). Broad patents straddle both stages; narrow patents specialise in one. This is the sharper form of H1: scope is associated not merely with the presence of a front-end symbol but with the *combination* of front-end and back-end claims in one invention.

The year trend reveals what has changed over time. The relative risk of a process-only patent falls by about 8.7% per year (RRR 0.913, p < 0.01), whereas the relative risk of an integrated patent is flat (RRR 0.993, n.s.). What has declined, therefore, is pure front-end claiming, not boundary spanning: integrated claiming has held its share relative to assembly-level claiming throughout the period. This is the pattern H2 anticipated, and it refines the binary result. The negative year coefficient in Table 4 reflects the disappearance of process-only inventions, not a retreat from the boundary.

**Table 5.** Multinomial logit of strategy type (base = assembly-only). Relative-risk ratios; standard errors in parentheses; \* p < 0.10, \*\* p < 0.05, \*\*\* p < 0.01.

| | Integrated | (SE) | Process-only | (SE) |
|---|:---:|:---:|:---:|:---:|
| Filing year (yc) | 0.993 | (0.015) | 0.913\*\*\* | (0.013) |
| Breadth | 1.403\*\*\* | (0.043) | 0.905\*\* | (0.044) |
| China (CN) | 1.205 | (0.280) | 2.687\*\*\* | (0.740) |
| Korea (KR) | 1.166 | (0.444) | 1.906 | (0.799) |
| Taiwan (TW) | 1.312 | (0.422) | 2.102\* | (0.857) |
| Japan (JP) | 1.046 | (0.583) | 0.086\*\*\* | (0.070) |
| EPO (EP) | 1.307 | (0.380) | 0.858 | (0.376) |
| PCT (WO) | 1.126 | (0.302) | 1.997\*\* | (0.645) |
| Other | 2.326 | (1.585) | 1.901 | (1.480) |
| N = 928; log-likelihood = −774.5; AIC = 1,589.0 | | | | |

The jurisdictional contrasts are concentrated in the process-only branch. The Chinese office is far more likely than the U.S. office to host a process-only patent (RRR 2.687, p < 0.01), with Taiwan (RRR 2.102, p < 0.10) and the PCT route (RRR 1.997, p < 0.05) also elevated, whereas the Japanese office almost never does (RRR 0.086, p < 0.01). None of the office coefficients in the integrated branch is significant: the propensity to file boundary-spanning patents does not differ systematically across offices once scope and timing are controlled. Fig. 7 plots the predicted probability of a process-only strategy by office at the means of the other covariates: roughly one in four Chinese-office applications is predicted to be process-only, against about one in seven at the U.S. office and about one in fifty at the Japanese office. H3a and the strategy component of H3b are supported.

### 5.4 Summary of hypothesis tests and robustness

Table 6 summarises the evidence. H1 is supported in both the binary and the multinomial specifications. H2 is supported: process-only claiming declines while integrated claiming holds its share. H3a is supported in both specifications, and H3b is supported for the process-only form (strongly) and for breadth (at the 10% level), with the binary-logit coefficient for Japan in the expected direction but imprecise.

**Table 6.** Summary of hypotheses and results.

| Hypothesis | Prediction | Evidence | Verdict |
|---|---|---|---|
| H1 | Broader scope → boundary spanning | OR 1.267\*\*\* (Table 4); RRR integrated 1.403\*\*\*, process-only 0.905\*\* (Table 5) | Supported |
| H2 | Process-only declines over time; integrated does not | RRR process-only 0.913\*\*\* per year; RRR integrated 0.993 (n.s.) | Supported |
| H3a | Chinese-office filings more process-oriented | OR 1.650\*\* (Table 4); RRR process-only 2.687\*\*\* (Table 5); breadth IRR 0.785\*\*\* (Table 3) | Supported |
| H3b | Japanese-office filings broader, rarely process-only | Breadth IRR 1.270\* (Table 3); RRR process-only 0.086\*\*\* (Table 5); OR 0.483 (n.s.) (Table 4) | Largely supported |

Several checks bear on the robustness of these results. First, the choice of count model does not drive the scope findings: the sign and significance of the year and Chinese-office effects are identical across OLS, robust OLS, Poisson and negative binomial specifications (Table B1); only the Japanese-office coefficient loses precision as the standard errors are corrected, which is why we report it at the 10% level. Second, the 2023–2024 downturn in filings is a publication-lag artefact; because the year trend is estimated over the full 1968–2024 period and the models control for year linearly, the truncation affects the level of recent counts rather than the direction of the estimated trend, but we caution against extrapolating the process-only decline into the truncated years. Third, the multinomial results are read as relative tendencies against the assembly-only base, in keeping with the independence-of-irrelevant-alternatives assumption; the binary logit, which does not require that assumption, yields the same qualitative conclusions for breadth, time and the Chinese office.

## 6. Discussion

### 6.1 Theoretical implications

*Convergence within an industry.* The first implication concerns the scope of convergence theory. The literature reviewed in Section 2.1 treats convergence as something that happens *between* industries, and its stage models describe how knowledge from one industry migrates into another until the industries themselves merge (Hacklin et al., 2009; Curran and Leker, 2011). Our results show that the same conceptual apparatus applies to a boundary that runs *through* an industry—between the fabrication and assembly stages of semiconductor manufacturing—and that the boundary can be observed at the level of the individual invention. Nearly two-thirds of hybrid-bonding patents claim front-end technology, and the modal patent is integrated. If one takes Jacobides et al.'s (2006) view that industry architectures are templates for the division of labour, then intra-industry convergence of this kind is the mechanism by which a template is renegotiated: the interface between stages ceases to be stable, and inventions begin to claim both sides of it.

*Scope as the micro-mechanism of fusion.* The second implication concerns the mechanism. Kodama's (1992) technology fusion was formulated at the level of the firm's R&D organisation; convergence indices are formulated at the level of the field. Our patent-level evidence locates the mechanism in the *scope of the individual invention*: patents that span more technology classes are the ones that combine front-end and back-end claims, and narrow patents specialise in one stage or the other. This is consistent with Lerner's (1994) finding that scope carries value and with Ziedonis's (2004) account of broad portfolios in fragmented technology markets, but it adds a convergence interpretation: in a technology whose yield depends on capabilities from an adjacent stage, breadth of scope is not only a protective strategy but the form that fusion takes in the claims.

*Recomposition rather than accumulation.* The third implication concerns dynamics. A naïve reading of convergence would predict that the share of boundary-spanning patents rises over time. It does not: integrated claiming has held its share, while pure front-end claiming has declined. The pattern is better described as *recomposition*. Early in the technology's life, the inventions that mattered were fabrication steps—planarisation, surface activation, anneal—and they were claimed as process inventions. As the technology matured and moved from W2W to D2W applications, invention shifted toward bonded structures and integrated stacks, and process knowledge became something a patent *includes* rather than something it *is*. This is the co-evolutionary logic of Hacklin et al. (2009) observed in a single technology: convergence proceeds not by more patents crossing the boundary but by the boundary-crossing content of patents changing form. It also explains why the negative year coefficient in the binary model should not be read as a retreat from the front end.

*Industry architecture and catch-up leave an imprint on claiming.* The fourth implication connects the patent-level results to the strategy literature. The jurisdictional contrasts are concentrated in the process-only branch and are consistent with the readings advanced in Section 2.4. Filings at the Chinese office are narrow and disproportionately process-only, the profile of a catch-up posture that internalises specific fabrication steps before claiming whole integrated stacks (Lee and Lim, 2001); the ecosystem evidence on YMTC—licensing the originator's technology, then developing an independent W2W process with an unusually fast citation peak—illustrates the same posture at the firm level. Filings at the Japanese office are broad and essentially never process-only, consistent with equipment and materials suppliers that patent across the full process flow rather than around single steps. Korea and Taiwan sit near the U.S. baseline on scope and on the integrated branch, which is plausible for memory makers and a foundry competing head-on with U.S. assignees; their elevated process-only tendencies (significant for Taiwan at the 10% level) are consistent with the foundry-led internalisation of front-end packaging capability that TSMC's revealed technological advantage in damascene and planarisation codes exemplifies. Together these patterns show that the *architecture* of the industry—who occupies which stage, and from where—shapes *which* inventions span the boundary, an extension of Kapoor's (2013) argument that firms' integration choices shape industry architecture to the level of the claims they write.

### 6.2 Managerial and policy implications

For firms in the back end, the results carry a clear warning. The traditional assembly-and-test model rested on a stable interface with fabrication; in hybrid bonding that interface has dissolved, and the patents that define the technology reach into front-end processes. An assembly specialist that lacks planarisation and surface-chemistry competences will find that the most valuable claims are already occupied by foundries and equipment suppliers. The finding that boundary-spanning is associated with scope suggests that the relevant capability is not a single process step but the ability to claim a flow from wafer preparation to bonded stack.

For foundries and integrated device manufacturers, the results confirm that internalising advanced packaging is not merely a capacity decision but a knowledge-boundary decision in the sense of Kapoor and Adner (2012): firms whose knowledge extends across the boundary can write the integrated claims that the technology now rewards. For equipment and materials suppliers, the Japanese-office profile—broad, integration-oriented, never process-only—indicates a distinctive and defensible position at the interface.

For policy, the jurisdictional contrasts are instructive. The Chinese-office profile shows a catch-up strategy proceeding step by step, which is effective for internalising fabrication capability but leaves the integrated claims to others; industrial policies that reward patent counts (Dang and Motohashi, 2015) will reinforce this pattern. For Korea, whose memory makers depend on hybrid bonding for the next generations of HBM, the results imply that competitiveness in packaging will be decided by front-end competences—planarisation, surface chemistry, alignment metrology—and by the ability to combine them in integrated claims, not by packaging know-how alone.

### 6.3 Limitations and future research

Four limitations bound these claims. First, the jurisdiction variable is the filing office, not the applicant's nationality; cross-office differences mix the origin of invention with protection strategy. Linking applications to applicant identifiers and headquarters locations would allow the two to be separated and would permit firm fixed effects, which would sharpen every comparison made here. Second, the estimates are associational. Scope and strategy are plausibly determined jointly, and with no credible instrument in the data we do not attempt a causal identification. Third, our convergence signal is CPC co-classification, which is deterministic and reproducible but lags invention: text-based methods (Preschitschek et al., 2013; Zhu and Motohashi, 2022) can detect boundary spanning before examiners assign a front-end code, at the cost of dependence on a trained model. In a setting such as ours, where the boundary between H01L21 and H01L24 is institutionally sharp, we judged reproducibility the greater virtue; in less codified boundaries the trade-off may run the other way. Fourth, the co-classification structure of a patent is many-to-many—each application is a set of CPC symbols—and our binary and three-way encodings project that structure onto a single boundary. A hypergraph representation that retains higher-order co-occurrence, and a generality index in the tradition of Trajtenberg et al. (1997) and Petralia (2020), would allow the general-purpose character suggested by hybrid bonding's spread across logic, memory, image sensors and power devices to be tested directly (cf. Bresnahan and Trajtenberg, 1995; Hall and Trajtenberg, 2004).

Beyond these, the design invites replication. Any process technology in which a downstream stage begins to depend on upstream capabilities—advanced lithography in display manufacturing, cell-to-pack integration in batteries, additive manufacturing in aerospace assembly—could be examined with the same two-class co-classification rule and the same discrete-choice estimators. Comparative studies across such settings would show whether the recomposition dynamic observed here is general.

## 7. Conclusion

Technological convergence has been studied almost exclusively as a meeting of industries. This paper has shown that it also occurs across the stages of a single industry's value chain, that it can be observed in the claims of individual patents, and that its determinants can be estimated. In hybrid bonding, a back-end process whose performance depends on front-end capabilities, nearly two-thirds of patent applications claim fabrication-process technology, and the modal invention integrates both stages. Boundary spanning is strongly associated with technological scope; over time it has proceeded by recomposition—pure front-end claiming has given way to integrated claiming—rather than by a monotonic rise in cross-boundary claims; and it bears the imprint of industry architecture and catch-up, with Chinese-office filings narrow and process-oriented and Japanese-office filings broad and integration-oriented. The front end and the back end, long separate, are converging in the record of invention, and the convergence is patterned in ways that convergence theory, extended to the intra-industry case and joined to the literature on industry architecture, can explain.

---

## CRediT authorship contribution statement

**HyungKyu Lee:** Conceptualization, Methodology, Data curation, Formal analysis, Writing – original draft, Writing – review & editing, Visualization.

## Declaration of competing interest

The author declares that he has no known competing financial interests or personal relationships that could have appeared to influence the work reported in this paper.

## Funding

This research did not receive any specific grant from funding agencies in the public, commercial, or not-for-profit sectors.

## Data availability

The patent data were extracted from EPO PATSTAT Global (2025 edition) under licence and cannot be redistributed by the author. The extraction queries (CPC codes and keyword filters, Appendix A) and the estimation scripts are available from the author on request. Supplementary Material S1 reports the applicant-level indicators summarised in Table 1 and Figs. 1–2.

## Declaration of generative AI and AI-assisted technologies in the writing process

During the preparation of this work the author used an AI language model to assist with literature organisation, language editing and the structuring of the manuscript. All empirical analyses, interpretations and conclusions are the author's own. After using this tool, the author reviewed and edited the content as needed and takes full responsibility for the content of the publication.

## Acknowledgements

[To be added.]

---

## Appendix A. Classification scheme and search strategy

**Table A1.** CPC classes defining the front-end/back-end boundary.

| CPC | Title (abridged) | Representative technologies |
|---|---|---|
| H01L21 | Processes or apparatus adapted for the manufacture or treatment of semiconductor devices | Lithography, etching, deposition, ion implantation, chemical–mechanical planarisation, wafer cleaning and processing (front-end and back-end manufacturing processes) |
| H01L24 | Arrangements for connecting or disconnecting semiconductor bodies; methods or apparatus related thereto | Wire bonding, flip-chip bonding, through-silicon vias, hybrid bonding, solder bump/ball formation, redistribution (packaging interconnection) |
| H01L2224 | Indexing scheme for arrangements for connecting or disconnecting semiconductor bodies | Bonding metal, bond form, equipment (e.g. H01L2224/08145: direct bonding combining metal–metal and dielectric–dielectric bonds) |

The hybrid-bonding population was retrieved in two steps: (1) CPC first-level filter on H01L21 and H01L24 with their indexing codes; (2) keyword filter combining *hybrid bonding*, *direct bonding*, *Cu–Cu*, *copper to copper* and *metal-oxide bond* (Fig. A1). A comparison population for HBM (about 2,330 records) was built with the same procedure using H01L21, H01L24, H01L2224 and H01L2924 with the keywords *high bandwidth memory*, *HBM*, *wide I/O memory*, *stacked memory die* and *memory stack*; it is used only for the descriptive comparison in Fig. 1.

**Fig. A1.** Search strategy for the hybrid-bonding population (CPC first-level filter and keyword second-level filter).

![Fig. A1. Search strategy](figures/figA1_search_strategy.png)

## Appendix B. Full comparison of breadth models

**Table B1.** Determinants of technological breadth across specifications (N = 928; U.S. office is the reference; standard errors in parentheses; \* p < 0.10, \*\* p < 0.05, \*\*\* p < 0.01).

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
| ln(α) | | | | −1.316\*\*\* (0.076) |
| R² | 0.033 | 0.033 | — | — |
| AIC | 5,224.9 | 5,224.9 | 5,477.2 | 4,883.0 |

---

## Figure captions

**Fig. 1.** Annual patent filings for hybrid bonding (process) and high-bandwidth memory (product), 2010–2024. Source: applicant-level analysis of the same PATSTAT extraction (Supplementary Material S1). The 2023–2024 decline reflects publication lag.

![Fig. 1](figures/fig1_filing_trends_hb_vs_hbm.png)

**Fig. 2.** Ten largest applicants by number of hybrid-bonding patents (applicant-level analysis; subsidiaries consolidated to current parent, e.g. Ziptronix and Invensas under Adeia). Source: Supplementary Material S1.

![Fig. 2](figures/fig2_top10_applicants.png)

**Fig. 3.** The hybrid-bonding technology ecosystem: core process, accompanying processes, CPC classes, enabled structures and products, major applicants (with citation impact, science linkage and market coverage indicators) and filing-office tendencies. Constructed deterministically from the indicators in Table 1 and the estimates in Tables 3–5.

![Fig. 3](figures/fig3_ecosystem_network.png)

**Fig. 4.** Hybrid-bonding applications by filing year, 1968–2024 (N = 928). The 2023–2024 dip is a publication-lag artefact.

![Fig. 4](figures/fig4_applications_by_year.png)

**Fig. 5.** Predicted technological breadth by filing office (negative binomial, other covariates at their means; 95% confidence intervals).

![Fig. 5](figures/fig5_predicted_breadth.png)

**Fig. 6.** Receiver operating characteristic curve for the boundary-spanning logit (AUC = 0.707).

![Fig. 6](figures/fig6_roc.png)

**Fig. 7.** Predicted probability of a process-only strategy by filing office (multinomial logit, other covariates at their means; 95% confidence intervals).

![Fig. 7](figures/fig7_predicted_process_only.png)

---

## References

Adner, R., Kapoor, R., 2010. Value creation in innovation ecosystems: How the structure of technological interdependence affects firm performance in new technology generations. Strategic Management Journal 31 (3), 306–333.

Arden, W., Brillouët, M., Cogez, P., Graef, M., Huizing, B., Mahnkopf, R., 2010. "More-than-Moore" White Paper. International Technology Roadmap for Semiconductors (ITRS).

Athreye, S., Keeble, D., 2000. Technological convergence, globalisation and ownership in the UK computer industry. Technovation 20 (5), 227–245.

Bresnahan, T.F., Trajtenberg, M., 1995. General purpose technologies "Engines of growth"? Journal of Econometrics 65 (1), 83–108.

Bröring, S., Cloutier, L.M., Leker, J., 2006. The front end of innovation in an era of industry convergence: Evidence from nutraceuticals and functional foods. R&D Management 36 (5), 487–498.

Brown, C., Linden, G., 2009. Chips and Change: How Crisis Reshapes the Semiconductor Industry. MIT Press, Cambridge, MA.

Brusoni, S., Prencipe, A., Pavitt, K., 2001. Knowledge specialization, organizational coupling, and the boundaries of the firm: Why do firms know more than they make? Administrative Science Quarterly 46 (4), 597–621.

Cameron, A.C., Trivedi, P.K., 2013. Regression Analysis of Count Data, 2nd ed. Cambridge University Press, Cambridge.

Caviggioli, F., 2016. Technology fusion: Identification and analysis of the drivers of technology convergence using patent data. Technovation 55–56, 22–32.

Cho, Y., Kim, M., 2014. Entropy and gravity concepts as new methodological indexes to investigate technological convergence: Patent network-based approach. PLOS ONE 9 (6), e98009. https://doi.org/10.1371/journal.pone.0098009

Curran, C.-S., Bröring, S., Leker, J., 2010. Anticipating converging industries using publicly available data. Technological Forecasting and Social Change 77 (3), 385–395.

Curran, C.-S., Leker, J., 2011. Patent indicators for monitoring convergence – Examples from NFF and ICT. Technological Forecasting and Social Change 78 (2), 256–273.

Dang, J., Motohashi, K., 2015. Patent statistics: A good indicator for innovation in China? Patent subsidy program impacts on patent quality. China Economic Review 35, 137–155.

Ernst, D., 2005. Complexity and internationalisation of innovation—why is chip design moving to Asia? International Journal of Innovation Management 9 (1), 47–73.

Ernst, H., 2003. Patent information for strategic technology management. World Patent Information 25 (3), 233–242.

European Patent Office, 2025. PATSTAT Global (2025 edition) [Database]. EPO, Vienna.

Fai, F., von Tunzelmann, N., 2001. Industry-specific competencies and converging technological systems: Evidence from patents. Structural Change and Economic Dynamics 12 (2), 141–170.

Gambardella, A., Torrisi, S., 1998. Does technological convergence imply convergence in markets? Evidence from the electronics industry. Research Policy 27 (5), 445–463.

Geum, Y., Kim, M.-S., Lee, S., 2016. How industrial convergence happens: A taxonomical approach based on empirical evidences. Technological Forecasting and Social Change 107, 112–120.

Grimes, S., Du, D., 2022. China's emerging role in the global semiconductor value chain. Telecommunications Policy 46 (2), 101959. https://doi.org/10.1016/j.telpol.2020.101959

Hacklin, F., Battistini, B., von Krogh, G., 2013. Strategic choices in converging industries. MIT Sloan Management Review 55 (1), 65–73.

Hacklin, F., Marxt, C., Fahrni, F., 2009. Coevolutionary cycles of convergence: An extrapolation from the ICT industry. Technological Forecasting and Social Change 76 (6), 723–736.

Hall, B.H., Trajtenberg, M., 2004. Uncovering GPTs with Patent Data. NBER Working Paper No. 10901. National Bureau of Economic Research, Cambridge, MA.

Hall, B.H., Ziedonis, R.H., 2001. The patent paradox revisited: An empirical study of patenting in the U.S. semiconductor industry, 1979–1995. RAND Journal of Economics 32 (1), 101–128.

Hausman, J., Hall, B.H., Griliches, Z., 1984. Econometric models for count data with an application to the patents–R&D relationship. Econometrica 52 (4), 909–938.

Hosmer, D.W., Lemeshow, S., Sturdivant, R.X., 2013. Applied Logistic Regression, 3rd ed. Wiley, Hoboken, NJ.

Hu, A.G., Jefferson, G.H., 2009. A great wall of patents: What is behind China's recent patent explosion? Journal of Development Economics 90 (1), 57–68.

Hwang, I., 2020. The effect of collaborative innovation on ICT-based technological convergence: A patent-based analysis. PLOS ONE 15 (2), e0228616. https://doi.org/10.1371/journal.pone.0228616

Iyer, S.S., 2016. Heterogeneous integration for performance and scaling. IEEE Transactions on Components, Packaging and Manufacturing Technology 6 (7), 973–982.

Jacobides, M.G., Knudsen, T., Augier, M., 2006. Benefiting from innovation: Value creation, value appropriation and the role of industry architectures. Research Policy 35 (8), 1200–1221.

Jeong, S., Kim, J.-C., Choi, J.Y., 2015. Technology convergence: What developmental stage are we in? Scientometrics 104 (3), 841–871.

Kapoor, R., 2013. Persistence of integration in the face of specialization: How firms navigated the winds of disintegration and shaped the architecture of the semiconductor industry. Organization Science 24 (4), 1195–1213.

Kapoor, R., Adner, R., 2012. What firms make vs. what they know: How firms' production and knowledge boundaries affect competitive advantage in the face of technological change. Organization Science 23 (5), 1227–1248.

Karvonen, M., Kässi, T., 2013. Patent citations as a tool for analysing the early stages of convergence. Technological Forecasting and Social Change 80 (6), 1094–1107.

Khan, H.N., Hounshell, D.A., Fuchs, E.R.H., 2018. Science and research policy at the end of Moore's law. Nature Electronics 1, 14–21.

KnowMade/Yole Group, 2024. Hybrid Bonding Patent Landscape Analysis 2024 [Industry report]. KnowMade, Nantes.

Kodama, F., 1992. Technology fusion and the new R&D. Harvard Business Review 70 (4), 70–78.

Kwon, O., An, Y., Kim, M., Lee, C., 2020. Anticipating technology-driven industry convergence: Evidence from large-scale patent analysis. Technology Analysis & Strategic Management 32 (4), 363–378.

Langlois, R.N., Steinmueller, W.E., 1999. The evolution of competitive advantage in the worldwide semiconductor industry, 1947–1996. In: Mowery, D.C., Nelson, R.R. (Eds.), Sources of Industrial Leadership: Studies of Seven Industries. Cambridge University Press, Cambridge, pp. 19–78.

Lau, J.H., 2021. State-of-the-art and outlooks of chiplets heterogeneous integration and hybrid bonding. Journal of Microelectronics and Electronic Packaging 18 (4), 145–160.

Lau, J.H., 2022. Recent advances and trends in advanced packaging. IEEE Transactions on Components, Packaging and Manufacturing Technology 12 (2), 228–252.

Lee, K., Lim, C., 2001. Technological regimes, catching-up and leapfrogging: Findings from the Korean industries. Research Policy 30 (3), 459–483.

Lee, W.S., Han, E.J., Sohn, S.Y., 2015. Predicting the pattern of technology convergence using big-data technology on large-scale triadic patents. Technological Forecasting and Social Change 100, 317–329.

Lerner, J., 1994. The importance of patent scope: An empirical analysis. RAND Journal of Economics 25 (2), 319–333.

Macher, J.T., Mowery, D.C., 2004. Vertical specialization and industry structure in high technology industries. In: Baum, J.A.C., McGahan, A.M. (Eds.), Business Strategy over the Industry Lifecycle. Advances in Strategic Management, vol. 21. Emerald, Bingley, pp. 317–356.

Marco, A.C., Sarnoff, J.D., deGrazia, C.A.W., 2019. Patent claims and patent scope. Research Policy 48 (9), 103790.

McFadden, D., 1974. Conditional logit analysis of qualitative choice behavior. In: Zarembka, P. (Ed.), Frontiers in Econometrics. Academic Press, New York, pp. 105–142.

Narin, F., Hamilton, K.S., Olivastro, D., 1997. The increasing linkage between U.S. technology and public science. Research Policy 26 (3), 317–330.

Narin, F., Noma, E., Perry, R., 1987. Patents as indicators of corporate technological strength. Research Policy 16 (2–4), 143–155.

No, H.J., Park, Y., 2010. Trajectory patterns of technology fusion: Trend analysis and taxonomical grouping in nanobiotechnology. Technological Forecasting and Social Change 77 (1), 63–75.

Petralia, S., 2020. Mapping general purpose technologies with patent data. Research Policy 49 (7), 104013.

Preschitschek, N., Niemann, H., Leker, J., Moehrle, M.G., 2013. Anticipating industry convergence: Semantic analyses vs IPC co-classification analyses of patents. Foresight 15 (6), 446–464.

Rosenberg, N., 1963. Technological change in the machine tool industry, 1840–1910. Journal of Economic History 23 (4), 414–443.

Sakakibara, M., Branstetter, L., 2001. Do stronger patents induce more innovation? Evidence from the 1988 Japanese patent law reforms. RAND Journal of Economics 32 (1), 77–100.

Sick, N., Bröring, S., 2022. Exploring the research landscape of convergence from a TIM perspective: A review and research agenda. Technological Forecasting and Social Change 175, 121321.

Sick, N., Preschitschek, N., Leker, J., Bröring, S., 2019. A new framework to assess industry convergence in high technology environments. Technovation 84–85, 48–58.

Soete, L., 1987. The impact of technological innovation on international trade patterns: The evidence reconsidered. Research Policy 16 (2–4), 101–130.

Song, C.H., Elvers, D., Leker, J., 2017. Anticipation of converging technology areas — A refined approach for the identification of attractive fields of innovation. Technological Forecasting and Social Change 116, 98–115.

Trajtenberg, M., Henderson, R., Jaffe, A., 1997. University versus corporate patents: A window on the basicness of invention. Economics of Innovation and New Technology 5 (1), 19–50.

Zhu, C., Motohashi, K., 2022. Identifying the technology convergence using patent text information: A graph convolutional networks (GCN)-based approach. Technological Forecasting and Social Change 176, 121477.

Ziedonis, R.H., 2004. Don't fence me in: Fragmented markets for technology and the patent acquisition strategies of firms. Management Science 50 (6), 804–820.
