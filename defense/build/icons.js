// Render Font Awesome 6 icons (react-icons) to PNG for the defense deck.
// Usage: node icons.js  -> icons/<Name>_<RRGGBB>.png (512 px)
const React = require('react');
const ReactDOMServer = require('react-dom/server');
const sharp = require('sharp');
const fs = require('fs');
const fa = require('react-icons/fa6');

const NAMES = ['FaMicrochip', 'FaMemory', 'FaRobot', 'FaServer', 'FaLayerGroup', 'FaIndustry', 'FaMagnifyingGlass',
  'FaBookOpen', 'FaLightbulb', 'FaChartLine', 'FaDatabase', 'FaCodeMerge', 'FaCircleNodes', 'FaFlask',
  'FaFilter', 'FaClockRotateLeft', 'FaRoad', 'FaGears', 'FaCubes', 'FaBuilding', 'FaHandshake', 'FaLink',
  'FaFileLines', 'FaScaleBalanced', 'FaTags', 'FaCopy', 'FaShuffle', 'FaHourglassHalf', 'FaGlobe',
  'FaArrowRightLong', 'FaCircleQuestion', 'FaShieldHalved', 'FaUsers', 'FaFont', 'FaBullseye', 'FaSitemap'];
const COLORS = ['FFFFFF', '13294B', 'C9A227', '2563C9', '7C3AED', 'EA7A28', '5B6475'];

(async () => {
  fs.mkdirSync('icons', { recursive: true });
  for (const n of NAMES) {
    if (!fa[n]) { console.log('missing', n); continue; }
    for (const c of COLORS) {
      const svg = ReactDOMServer.renderToStaticMarkup(React.createElement(fa[n], { size: 512, color: '#' + c }));
      await sharp(Buffer.from(svg)).resize(512, 512, { fit: 'contain', background: { r: 0, g: 0, b: 0, alpha: 0 } })
        .png().toFile(`icons/${n}_${c}.png`);
    }
  }
  console.log('icons done');
})();
