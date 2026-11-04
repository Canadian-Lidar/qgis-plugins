# CloudPro – sources of the processing methods

CloudPro's automatic filters cover the classic cell-statistics, clip and cluster filters of hydrographic and Lidar cleaning software, and add the peer-reviewed methods below. The citations were checked on publisher or index pages on 2 Nov 2026. "Not confirmed" marks a detail that could not be checked.

| Filter in CloudPro | Method | Citation | DOI |
| --- | --- | --- | --- |
| Statistical outlier (SOR) | k-NN mean distance, global mean + α·σ | Rusu, Marton, Blodow, Dolha, Beetz (2008). Towards 3D point cloud based object maps for household environments. *Robotics and Autonomous Systems* 56(11):927–941 | 10.1016/j.robot.2008.08.005 |
| Snow / rain / dust – DROR | Radius outlier removal, radius = max(SRmin, β·r·α) | Charron, Phillips, Waslander (2018). De-noising of lidar point clouds corrupted by snowfall. *15th Conf. on Computer and Robot Vision (CRV)*, pp. 254–261 | 10.1109/CRV.2018.00043 (not confirmed) |
| Snow / rain / dust – LIOR | Intensity gate, then ROR on dim points only | Park, Park, Kim (2020). Fast and accurate desnowing algorithm for LiDAR point clouds. *IEEE Access* 8:160202–160212 | 10.1109/ACCESS.2020.3020266 |
| Snow / rain / dust – DIOR | Intensity gate, then DROR on dim points only | Roriz et al. (2022). DIOR: a hardware-assisted weather denoising solution for LiDAR point clouds. *IEEE Sensors J.* 22(2):1621–1628 | 10.1109/JSEN.2021.3133873 |
| Progressive morphological filter (PMF) | Openings with a growing window, slope-dependent threshold | Zhang K., Chen, Whitman, Shyu, Yan, Zhang C. (2003). *IEEE TGRS* 41(4):872–882 | 10.1109/TGRS.2003.810682 |
| Simple morphological filter (SMRF) | Progressive opening + interpolated DEM + slope-scaled threshold | Pingel, Clarke, McBride (2013). *ISPRS J. Photogramm. Remote Sens.* 77:21–30 | 10.1016/j.isprsjprs.2012.12.002 |
| Inverted cloth (CSF) | Cloth simulation on the inverted cloud | Zhang W., Qi, Wan, Wang H., Xie, Wang X., Yan (2016). *Remote Sensing* 8(6):501 | 10.3390/rs8060501 |
| Slope / spike filter | Slope-based filtering | Vosselman (2000). Slope based filtering of laser altimetry data. *IAPRS* 33(B3/2):935–942 | – |
| Geometric features | Dimensionality features from local eigenvalues | Demantké, Mallet, David, Vallet (2011). *ISPRS Archives* XXXVIII-5/W12:97–102; Weinmann, Jutzi, Hinz, Mallet (2015). *ISPRS J.* 105:286–304 | 10.5194/isprsarchives-XXXVIII-5-W12-97-2011; 10.1016/j.isprsjprs.2015.01.016 |
| Moving objects / ghosts (multi-pass) | Free-space voxel traversal along the laser rays | Schauer, Nüchter (2018). The Peopleremover – removing dynamic objects from 3-D point cloud data by traversing a voxel occupancy grid. *IEEE RA-L* 3(3) | 10.1109/LRA.2018.2801797 |
| (related) | Range-image removal and revert | Kim G., Kim A. (2020). Remove, then revert. *IEEE/RSJ IROS* | 10.1109/IROS45743.2020.9340856 |
| Isolated cluster | Connected components (voxel graph); density clustering | Ester, Kriegel, Sander, Xu (1996). DBSCAN. *Proc. KDD-96*, AAAI Press | – |
| Clip intensity, range-normalised intensity colour | Range correction of intensity | Kashani, Olsen, Parrish, Wilson (2015). A review of LIDAR radiometric processing. *Sensors* 15(11):28099–28128 | 10.3390/s151128099 |
| Vertical plane (walls, reflections behind glass) | RANSAC plane detection | Fischler, Bolles (1981), *Comm. ACM* 24(6); Schnabel, Wahl, Klein (2007). Efficient RANSAC for point-cloud shape detection. *Computer Graphics Forum* 26(2):214–226. Reflection idea: Yun, Sim (2018), Reflection removal for large-scale 3D point clouds, *CVPR 2018* | 10.1111/j.1467-8659.2007.01016.x; 10.1109/CVPR.2018.00483 |
| Surface fit | Robust local surface fit (moving least squares) | Fleishman, Cohen-Or, Silva (2005). Robust moving least-squares fitting with sharp features. *ACM TOG* 24(3):544–552 | 10.1145/1073204.1073227 |
| Ground filter validation (Type I / II errors) | ISPRS filter benchmark | Sithole, Vosselman (2004). *ISPRS J.* 59(1–2):85–101; Meng, Currit, Zhao (2010). *Remote Sensing* 2(3):833–860 | 10.1016/j.isprsjprs.2004.05.004; 10.3390/rs2030833 |
| Eye-dome lighting (3-D view) | Non-photorealistic depth shading | Boucheny (2009). *Visualisation scientifique de grands volumes de données*, PhD thesis, Université Grenoble 1 | – |
| Road markings | Intensity image, Otsu threshold per distance band from the trajectory, morphological closing | Guan, Li, Yu, Wang, Chapman, Yang (2014). Using mobile laser scanning data for automated extraction of road markings. *ISPRS J. Photogramm. Remote Sens.* 87:93–107 | 10.1016/j.isprsjprs.2013.11.005 |
| Curbs | Height jump with a steep face over a short window, continuous along the road | Yang, Fang, Li (2013). Semi-automated extraction and delineation of 3D roads of street scene from mobile laser scanning point clouds. *ISPRS J.* 79:80–93 | 10.1016/j.isprsjprs.2013.01.016 |
| Poles | Small isolated sections in horizontal voxel layers, stacked vertically | Cabo, Ordóñez, García-Cortés, Martínez (2014). An algorithm for automatic detection of pole-like street furniture objects from mobile laser scanner point clouds. *ISPRS J.* 87:47–56; Lehtomäki, Jaakkola, Hyyppä, Kukko, Kaartinen (2010). *Remote Sensing* 2(3):641–664 | 10.1016/j.isprsjprs.2013.10.008; 10.3390/rs2030641 |
| Traffic signs | Retro-reflective intensity, planar upright cluster of sign size and height | Riveiro, Díaz-Vilariño, Conde-Carnero, Soilán, Arias (2016). Automatic segmentation and shape-based classification of retro-reflective traffic signs from mobile LiDAR data. *IEEE JSTARS* 9(1):295–303 | 10.1109/JSTARS.2015.2461680 |
| Power lines | High points with free space above and below, clustered, catenary fit | Guan, Yu, Li, Ji, Zhang (2016). Extraction of power-transmission lines from vehicle-borne lidar data. *Int. J. Remote Sens.* 37(1):229–247 | 10.1080/01431161.2015.1125549 |
| Manhole covers | Intensity-image blobs that differ from the road, round and of cover size | Guan, Yu, Li, Liu (2014). Automated detection of urban road manhole covers using mobile laser scanning data. *Remote Sens. Lett.* 5(12):1042–1050; Yu, Li, Guan, Wang, Yang (2014). *IEEE GRSL* | 10.1080/2150704X.2014.994716; 10.1109/LGRS.2014.2301195 |
| Guardrails | Height band, clustering, line fit, occupied slices along the line | Gao, Yang, Wu, Wen, Jiang (2022). Automatic extraction of road guardrails from mobile laser scanning point clouds. *IEEE T-ITS* 23(2):1572–1577; Huang et al. (2020). *Sensors and Materials* 32(11):4005 | 10.1109/TITS.2020.3025067; 10.18494/SAM.2020.3118 |
| (review) | Road-object extraction from mobile laser scanning | Ma, Li, Li, Zhong, Chapman (2018). Mobile laser scanned point-clouds for road object detection and extraction: a review. *Remote Sensing* 10(10):1531 | 10.3390/rs10101531 |

## Changes from the papers

- **Ground filters (SMRF, PMF, CSF).** The surface starts from a *robust* minimum: the 3rd-lowest point of each cell, with deeper pits lifted to a neighbourhood median. A plain minimum let multipath "low points" below the road drag the surface down. On the simulated street at the real Hesai point rate, Type I error was 34–57 % with the plain minimum and 0.00 % with the robust one (3rd-lowest point per cell and pit lifting).
- **Moving objects.** Rays are sampled every half voxel rather than traversed exactly with 3-D DDA. A pass protects the 26 voxels around its own returns, so rays that pass through tree crowns are not counted as free space. Using sensor positions from the trajectory makes this practical for a mobile mapper.
- **Feature extraction.** All seven classify by default and need ground: without a ground class they use a robust minimum surface and say so. *Poles* ignore voxels with fewer than 3 points when testing isolation, because one snowflake beside a pole otherwise breaks it; stacks that end in a large crown are left as tree trunks. *Power lines* replace per-point linearity with a free-space test (nothing within 1 m above or below the voxel column), because parallel conductors 0.6 m apart make neighbourhoods planar, and need 3 points per voxel so snow in the air does not chain the clusters together. *Guardrails* add an empty-above test that rejects walls and vehicles in the same height band. *Road markings and manholes* keep to the ground class when there is one.
- **DROR α** must match the azimuth step of the data. That is 0.18° for the XT-32 at 600 rpm with all packets kept. Multiply it by N when importing with "keep every Nth packet".

## Sensor figures used by "Tune to the sensor precision"

- Hesai XT32 product page (range precision 0.5 cm 1σ, accuracy ±1 cm, 0.18° / 1° resolution): https://hesaitech.com/product/xt32
- Advanced Navigation Certus Evo (RTK 0.01 m H / 0.015 m V; roll/pitch 0.03°, heading 0.05° dual antenna; post-processed 0.01°): https://www.advancednavigation.com/product/certus-evo

## Large point cloud handling (ideas taken from TopoDOT / Bentley practice)

- Display budget per view with progressive refinement, TopoDOT 2024.1 release notes: https://blog.topodot.com/1043-topodot-version-2024-1-release-highlights/
- Density and "limit incoming points" settings, Ohio DOT survey point cloud training guide: https://ftp.dot.state.oh.us/pub/cadd/caddsync/ODOTcadd/Standards/ODOT/Training/Guides/ODOT_Survey_Point_Cloud_Training_Guide.pdf
- Stepping stations with [ and ] along a guideline (same guide); splitting long jobs (TopoDOT QA article): https://blog.topodot.com/establishing-requirements-extracting-metrics-and-evaluating-quality-of-lidar-data/
