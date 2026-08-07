# frontend/ — moved

This UI now lives in the **figureskatingtools-site** repo (`site/src/scoremodifier/`,
`site/scoremodifier/index.html`) and is served at
`https://figureskatingtools.com/scoremodifier/` by the shared router Web App.
Nothing here is deployed any more — this directory is kept only until the old
`scoremodifier.figureskatingtools.com` Web App is torn down. See `PROXY-CONTRACT.md`
for how the router reaches this repo's Function App.
