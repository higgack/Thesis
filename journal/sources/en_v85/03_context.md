## 3. Research context: Hybrid bonding technology and industry

### 3.1 Technical characteristics of hybrid bonding

In hybrid bonding, two planar surfaces with copper pads embedded in a dielectric are brought together; the dielectric is bonded first at room temperature, and the copper pads are then joined by low-temperature annealing to form permanent electrical connections. Because no solder bumps are used, the interconnect pitch can be reduced below 10 µm, which is difficult to achieve with microbump flip-chip methods, and reaches below 1 µm in research settings (Lau, 2021, 2022). Wafer-to-wafer (W2W) bonding, which bonds entire wafers, was commercialized first in CMOS image sensors and 3D NAND, whereas die-to-wafer (D2W) bonding, which bonds individual dies to a wafer, is required to implement chiplets, HBM, and 3D logic. Figure 1 organizes hybrid bonding into four layers: enabling processes, bonding configurations, device structures, and applications.

![](figures/fig_framework_psp_en_v64.png)

**Figure 1.** Enabling processes, bonding configurations, device structures, and applications of hybrid bonding (prepared by the authors on the basis of the technical descriptions in Lau (2021, 2022)).

### 3.2 Interdependence between fabrication processes and bonding technologies

The steps that determine the yield of hybrid bonding (chemical mechanical polishing (CMP), which polishes surfaces to sub-nanometer roughness; plasma surface activation; particle control; and precise alignment) all belong to the semiconductor fabrication-process class (H01L21) of the CPC. The enabling processes in Figure 1 correspond mainly to H01L21, and the bonding configurations and interconnect structures mainly to the bonding and interconnect class (H01L24). In the actual D2W process flow as well, interlayer dielectric deposition, damascene etching, copper barrier and seed deposition, copper plating, and chemical mechanical polishing proceed in sequence before the dies are singulated and bonded, and all of these are processes and equipment used in the front end (Applied Materials, 2025). Hybrid bonding inventions must therefore combine front-end knowledge of planarization, surface chemistry, and alignment with back-end knowledge of die handling, stacking, and packaging, knowledge that has long been divided among different production stages and often among different firms.

The industry landscape shows the same interdependence. According to the industry analysis of KnowMade (2024), the applicants leading patent activity in hybrid bonding are TSMC, Adeia, YMTC, Intel, and Samsung, a group that includes a licensing firm holding foundational technology together with foundries and memory manufacturers that operate advanced packaging directly. The knowledge this technology requires is thus distributed across organizations that were separated by the vertical division of labor, which makes hybrid bonding a suitable case for observing cross-stage knowledge integration.
