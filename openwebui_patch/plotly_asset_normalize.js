const LEGACY_PLOTLY_SCRIPT =
	/(<script\b[^>]*?\s+src\s*=\s*)(["'])http:\/\/127\.0\.0\.1:8000\/assets\/plotly-6\.6\.0\.min\.js\2/g;

/** 只正規化已知舊版 BadmintonAI Plotly script URL，其他 HTML 原樣保留。 */
export function normalizeLegacyPlotlyAsset(html) {
	if (typeof html !== 'string') return html;
	return html.replace(LEGACY_PLOTLY_SCRIPT, (_match, prefix, quote) =>
		`${prefix}${quote}/badmintonai/assets/plotly-6.6.0.min.js${quote}`
	);
}
