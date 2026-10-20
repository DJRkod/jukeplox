// Shared synthetic catalog and fetch mock for the rig's reproduction harnesses.
//
// Shared deliberately. The first harness written for this rig had two field-name
// bugs -- artists carrying `name` where the module sorts and buckets on `title`,
// and endpoints returning a wrapper object where the module reads a bare array.
// Either one produced an empty list that a presence check would have called
// healthy. Two harnesses each writing their own row shapes is how that comes
// back, so the shapes live here once.
//
// The shapes mirror app/catalog/views.py exactly:
//   _artist -> {id, title, thumb, release_count}
//   _album  -> {id, title, artist, year, thumb, subtype, track_count, sources}
// and both browse endpoints return a BARE ARRAY, not a wrapper.
//
// This catalog is the ONLY non-real part of any harness that uses it. The browse
// module, the stylesheet and the DOM are the shipped ones, which is what makes
// these pages safe to serve anywhere and what makes their results mean
// something.

(function () {
  const LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split('');

  function makeArtists(n) {
    const out = [];
    for (let i = 0; i < n; i++) {
      const L = LETTERS[i % 26];
      out.push({
        id: 'ar' + i,
        title: L + 'rtist ' + String(i).padStart(4, '0'),
        thumb: null,
        release_count: 3,
      });
    }
    return out;
  }

  function makeAlbums(n) {
    const out = [];
    for (let i = 0; i < n; i++) {
      const L = LETTERS[i % 26];
      out.push({
        id: 'al' + i,
        title: L + 'lbum ' + String(i).padStart(5, '0'),
        artist: L + 'rtist ' + String(i % 300).padStart(4, '0'),
        year: 1990 + (i % 35),
        thumb: null,
        subtype: 'album',
        track_count: 10,
        sources: null,
      });
    }
    return out;
  }

  function makeTracks(albumId, n) {
    const out = [];
    for (let i = 0; i < n; i++) {
      out.push({
        id: albumId + '-t' + i,
        title: 'Track ' + String(i + 1).padStart(2, '0'),
        artist: 'Synthetic Artist',
        album: albumId,
        duration: 180 + i,
        track_number: i + 1,
        disc_number: 1,
        thumb: null,
        server_name: 'synthetic',
      });
    }
    return out;
  }

  const params = new URLSearchParams(location.search);
  const CATALOG = {
    artists: makeArtists(Number(params.get('artists') || 300)),
    albums: makeAlbums(Number(params.get('albums') || 1500)),
  };

  // Mocked fetch. Anything unrecognised answers `{}` rather than throwing, so
  // an endpoint added to the app later degrades to "no data" instead of
  // breaking the harness with an error that looks like a product defect.
  window.fetch = function (url) {
    const u = String((url && url.url) || url);
    let body = {};
    if (u.includes('/api/browse/artists')) body = CATALOG.artists;
    else if (/\/api\/browse\/albums\/[^/]+\/tracks/.test(u)) {
      body = makeTracks(u.split('/albums/')[1].split('/')[0], 10);
    } else if (u.includes('/api/browse/albums')) body = CATALOG.albums;
    else if (u.includes('/api/appearance')) {
      body = {
        scheme: 'gold-rush', rail_mode: 'vanilla',
        // ?view=tile starts the harness in tile layout. Tile mode is the only
        // one where row height is genuinely unstable: tiles wrap, art loads in
        // after render, and the rail's padding write changes how many fit per
        // row -- all three move content above the viewport, which is exactly
        // when a remembered pixel offset stops meaning anything.
        view: params.get('view') === 'tile' ? 'tile' : 'list',
        rail_alpha_mode: 'english', rail_artist_threshold: 2,
        rail_album_threshold: 2, rating_style: 'stars',
        surprise_me_enabled: false,
      };
    } else if (u.includes('/api/scan-status')) body = { scanning: false };
    else if (u.includes('/api/pattern-rules')) body = { rules: [] };
    else if (u.includes('/api/play-counts')) body = [];
    // Bare arrays, not wrappers. `/api/servers` returning {servers: []} made
    // the module throw `(_serverMeta || []).forEach is not a function` -- the
    // third field-shape bug this harness has had, and the reason the shapes
    // live in one place now.
    else if (u.includes('/api/servers')) body = [];
    return Promise.resolve(new Response(JSON.stringify(body), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }));
  };
  window.fetch.__isHarnessMock = true;

  window.__catalog = CATALOG;
})();
