## 3. Research context: Hybrid bonding technology and industry

Before the cross-stage technology convergence discussed in Section 2 is analyzed empirically, this section examines the technical characteristics of hybrid bonding, the object of analysis (Section 3.1), as well as the interdependence between fabrication-process and bonding technologies and related industry trends (Section 3.2).

### 3.1 Technical characteristics of hybrid bonding

Hybrid bonding is a technology that brings together two planar surfaces with copper pads embedded in a dielectric, first bonds the dielectric at room temperature, and then bonds the copper pads through low-temperature annealing, thereby forming permanent electrical connections. Because no solder bumps are used, the interconnect pitch shrinks below 10 µm, which is difficult to achieve with microbump flip-chip technology, and reaches below 1 µm at the research stage (Lau, 2021, 2022). Depending on what is bonded, the technology takes two main forms. Wafer-to-wafer (W2W) bonding joins entire wafers and was first commercialized in CMOS image sensors and 3D NAND. Die-to-wafer (D2W) bonding joins individual dies to a wafer and is required for chiplets, HBM, and 3D logic. Figure 1 organizes the configuration of hybrid bonding into four tiers: enabling processes, bonding configurations, device structures, and applications.

![](figures/fig_framework_psp_en_v64.png)

**Figure 1.** Enabling processes, bonding configurations, device structures, and applications of hybrid bonding (prepared by the authors on the basis of the technical descriptions in Lau (2021, 2022)).

### 3.2 Interdependence between fabrication-process and bonding technologies

Because of these technical characteristics, fabrication-process and bonding technologies are closely interdependent in hybrid bonding. In both W2W and D2W bonding, the steps that determine yield, namely chemical mechanical polishing (CMP) to sub-nanometer surface roughness, plasma surface activation, particle control, and precise alignment, belong to the semiconductor fabrication-process class (H01L21) of the CPC (Section 4.1, Table 1). Accordingly, the enabling processes in Figure 1 correspond mainly to the fabrication-process class (H01L21), and the bonding configurations and interconnect structures correspond mainly to the bonding and interconnect class (H01L24). This interdependence is also evident in the actual process flow. Figure 2 shows the process flow of D2W hybrid bonding: before the dies are singulated and bonded, interlayer dielectric deposition, damascene etching, copper barrier and seed deposition, copper plating, and chemical mechanical polishing are performed in sequence, all of which are processes and equipment that have been used in the front end.

![](figures/fig_d2w_flow_amat_ko.png)

**Figure 2.** Process flow of die-to-wafer (D2W) hybrid bonding and the equipment used at each step (source: Applied Materials, 2025).

The industry landscape also reflects this technological interdependence. An independent industry analysis of hybrid bonding patents identifies TSMC, Adeia, YMTC, Intel, and Samsung as the applicants leading patent activity (KnowMade, 2024). The fact that licensing firms holding foundational technologies, foundries that operate their own advanced packaging, and memory manufacturers are jointly leading patent activity shows that bonding technology is no longer the exclusive domain of back-end specialists. These technological and industrial characteristics suggest that hybrid bonding is a suitable case for analyzing cross-stage technology convergence. The next section presents the data and methods used to analyze it empirically.
