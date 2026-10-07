const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");


try {
	const helperSource = fs.readFileSync(process.argv[2], "utf8")
		.replace("export function normalizeLegacyPlotlyAsset", "function normalizeLegacyPlotlyAsset");
	const context = {};
	vm.runInNewContext(
		`${helperSource}\nglobalThis.normalizeLegacyPlotlyAsset = normalizeLegacyPlotlyAsset;`,
		context
	);
	const normalizeLegacyPlotlyAsset = context.normalizeLegacyPlotlyAsset;
	const oldUrl = "http://127.0.0.1:8000/assets/plotly-6.6.0.min.js";
	const newUrl = "/badmintonai/assets/plotly-6.6.0.min.js";
	const oldEmbed = `<script src="${oldUrl}"></script>`;
	const newEmbed = `<script src="${newUrl}"></script>`;
	assert.equal(normalizeLegacyPlotlyAsset(oldEmbed), newEmbed);
	assert.equal(normalizeLegacyPlotlyAsset(newEmbed), newEmbed);
	for (const unrelated of [
		"http://127.0.0.1:8001/assets/plotly-6.6.0.min.js",
		"http://127.0.0.1:8000/assets/plotly-6.6.0.min.js?ignored=1",
		"http://localhost:8000/assets/plotly-6.6.0.min.js",
		"https://cdn.plot.ly/plotly-6.6.0.min.js",
		"http://127.0.0.1:8000/assets/other.js"
	]) {
		const html = `<script src="${unrelated}"></script>`;
		assert.equal(normalizeLegacyPlotlyAsset(html), html);
	}
	const textOnly = `<p>${oldUrl}</p>`;
	assert.equal(normalizeLegacyPlotlyAsset(textOnly), textOnly);
	console.log("Plotly asset normalization passed");
	} catch (error) {
	console.error(error);
	process.exitCode = 1;
}
