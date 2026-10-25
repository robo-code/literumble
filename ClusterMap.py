#!/usr/bin/env python
import json
import warnings

import numpy
from flask import request

import store
import structures


def _logit(m, eps=0.001):
    p = numpy.clip(m.astype(numpy.float32) / 100, eps, 1 - eps)  # float32 is plenty for a plot
    return numpy.log(p / (1 - p))


def _features(aps, surv):
    """Unit style vector per bot (column), in sd units: its surprise against each
    opponent (logit APS minus a strength-only Bradley-Terry fit by double-centring),
    then at half weight how much more it survived than that APS predicts. Weighted
    by sqrt(e(1-e)): 99.9 vs 0.1 says nothing about style, only that one bot is
    much stronger."""
    L = _logit(aps)
    fit = numpy.nanmean(L, 0)[None, :] + numpy.nanmean(L, 1)[:, None] - numpy.nanmean(L)
    e = 1 / (1 + numpy.exp(-fit))
    w = numpy.sqrt(e * (1 - e))
    Ls = _logit(surv)
    ok = ~numpy.isnan(L) & ~numpy.isnan(Ls)
    x, y = L[ok], Ls[ok]
    dx = x - x.mean()
    slope = dx.dot(y - y.mean()) / dx.dot(dx)
    surv_surprise = Ls - y.mean() - slope * (L - x.mean())
    surv_surprise /= numpy.nanstd(surv_surprise)
    X = numpy.nan_to_num(numpy.vstack([w * w * (L - fit), 0.5 * w * surv_surprise]))
    return X / (numpy.linalg.norm(X, axis=0) + 1e-12)


def _volatility_rank(aps, var):
    """0..1 rank of each bot's typical matchup sd, relative to what its scores allow."""
    p = numpy.clip(aps.astype(numpy.float32) / 100, 0.001, 0.999)
    t = numpy.nanmean(numpy.log(numpy.sqrt(var / (p * (1 - p))) + 1e-12), 0)
    t = numpy.where(numpy.isnan(t), numpy.nanmedian(t), t)
    return numpy.argsort(numpy.argsort(t)) / max(len(t) - 1.0, 1.0)


def spectral_embed(aps, surv, var, knn=10):
    """2D map of bots by style. All args are ``[j, i]`` = bot i vs bot j (NaN =
    unplayed), the BatchRankings APSs layout. Two bots are similar when their
    surprises point the same way, nudged toward bots of similar volatility.
    Returns ``[(x, y)]`` per bot: a t-SNE layout of the kNN similarity graph,
    started from its Laplacian eigenmap.
    """
    n = aps.shape[0]
    if n <= knn:
        return [(0.0, 0.0)] * n
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN rows/cols
        X = _features(aps, surv)
        vol = _volatility_rank(aps, var)
    C = X.T @ X
    del X
    C -= 0.17 * numpy.abs(vol[:, None] - vol[None, :])
    numpy.fill_diagonal(C, -numpy.inf)

    nearest = numpy.argsort(-C, 1)[:, :knn].ravel()
    rows = numpy.repeat(numpy.arange(n), knn)
    W = numpy.zeros((n, n))
    W[rows, nearest] = numpy.maximum(C[rows, nearest], 0)
    del C
    W = numpy.maximum(W, W.T)
    Wc = W + 1e-4 / n  # tiny global link keeps the graph connected
    numpy.fill_diagonal(Wc, 0)
    inv_sqrt_deg = 1 / numpy.sqrt(Wc.sum(1))
    Wc *= inv_sqrt_deg[:, None]
    Wc *= inv_sqrt_deg[None, :]
    vecs = numpy.linalg.eigh(Wc)[1][:, ::-1]
    del Wc

    # the spectrum is flat (~10 similar dims), so 2 eigenvectors alone clump small
    # families; t-SNE keeps neighbourhoods, the eigenmap init the global layout
    xy = _tsne(W, vecs[:, 1:3] * inv_sqrt_deg[:, None])
    return [(float(x), float(y)) for x, y in xy]


def _tsne(P, Y, iters=750, lr=200.0):
    """Exact t-SNE (van der Maaten 2008) for a given symmetric affinity P from
    init Y."""
    P = (P / P.sum()).astype(numpy.float32)
    Y = (Y / Y.std(0) * 1e-2 * numpy.sqrt(len(P))).astype(numpy.float32)
    upd = numpy.zeros_like(Y)
    gains = numpy.ones_like(Y)
    for it in range(iters):
        early = it < 250
        sq = (Y * Y).sum(1)
        Q = 1 / (1 + numpy.maximum(sq[:, None] + sq[None, :] - 2 * Y @ Y.T, 0))
        numpy.fill_diagonal(Q, 0)
        PQ = ((12.0 if early else 1.0) * P - Q / Q.sum()) * Q  # early exaggeration
        g = 4 * (PQ.sum(1)[:, None] * Y - PQ @ Y)
        gains = numpy.where(numpy.sign(g) != numpy.sign(upd), gains + 0.2, gains * 0.8).clip(0.01)
        upd = (0.5 if early else 0.8) * upd - lr * gains * g
        Y = Y + upd
    return Y


def cluster_map():
    game = request.args.get("game", "roborumble")
    dark = request.args.get("theme", "") == "dark"
    extraArgs = "&theme=dark" if dark else ""

    aps = store.load_aps(game)
    cmap = store.load_cluster_map(game)
    # bots that joined since the last batch run have no position yet
    points = sorted(([n, xy[0], xy[1], float(aps[n])]
                     for n, xy in cmap.items() if n in aps), key=lambda p: p[3])

    title = (structures.home_link("LITERUMBLE", dark) + " CLUSTER MAP - <a href=\"Rankings?game=" + game
             + extraArgs.replace("&", "&amp;") + "\">" + game.upper() + "</a> WITH " + str(len(points)) + " BOTS")
    out = [structures.header(game + " cluster map", title, dark)]
    if not points:
        out.append("No cluster map yet; it is computed by the batch rankings.</body></html>")
        return "".join(out)
    out.append("""<p>t-SNE of pairwise scores and survival, with overall strength factored out: nearby bots
beat and lose to the same opponents, and are about as consistent. Colour is APS. Click a dot for its details.</p>
<canvas id="map" width="700" height="700" style="border: black 1px solid; max-width: 100%; cursor: pointer;"></canvas>
<div style="display:flex;align-items:center;gap:6px;font-size:12px;max-width:700px">
<span id="lo"></span><div id="legend" style="flex:1;height:10px"></div><span id="hi"></span> APS</div>
<div id="tip" style="position:fixed;padding:1px 4px;font-size:12px;pointer-events:none;display:none"></div>
<script>
(function() {
  var P = """ + json.dumps(points).replace("</", "<\\/") + """;
  var game = """ + json.dumps(game).replace("</", "<\\/") + """, extra = '""" + extraArgs + """';
  var PL = ['#0d0887','#47039f','#7301a8','#9c179e','#bd3786','#d8576b','#ed7953','#fb9f3a','#fdca26','#f0f921'];
  var canvas = document.getElementById('map'), ctx = canvas.getContext('2d'), tip = document.getElementById('tip');
  var dark = document.body.classList.contains('dark');
  var S = canvas.width, M = 10;
  function range(k) { var a = P.map(function(p) { return p[k]; }); return [Math.min.apply(null, a), Math.max.apply(null, a)]; }
  var rx = range(1), ry = range(2), ra = range(3);
  document.getElementById('lo').textContent = ra[0].toFixed(2);
  document.getElementById('hi').textContent = ra[1].toFixed(2);
  document.getElementById('legend').style.background = 'linear-gradient(to right,' + PL.join(',') + ')';
  function hex(h) { return [1, 3, 5].map(function(i) { return parseInt(h.substr(i, 2), 16); }); }
  function plasma(t) {
    var f = Math.max(0, Math.min(1, t)) * (PL.length - 1), i = Math.min(PL.length - 2, Math.floor(f)), u = f - i;
    var a = hex(PL[i]), b = hex(PL[i + 1]);
    return 'rgb(' + a.map(function(v, k) { return Math.round(v + (b[k] - v) * u); }).join(',') + ')';
  }
  ctx.fillStyle = dark ? '#1e1e1e' : '#fff';
  ctx.fillRect(0, 0, S, S);
  var px = P.map(function(p) {
    var x = M + (p[1] - rx[0]) / (rx[1] - rx[0] || 1) * (S - 2 * M);
    var y = S - M - (p[2] - ry[0]) / (ry[1] - ry[0] || 1) * (S - 2 * M);
    ctx.fillStyle = plasma((p[3] - ra[0]) / (ra[1] - ra[0] || 1));
    ctx.beginPath(); ctx.arc(x, y, 3, 0, 6.283); ctx.fill();
    return [x, y];
  });
  function nearest(e) {
    var r = canvas.getBoundingClientRect(), sc = S / r.width;
    var mx = (e.clientX - r.left) * sc, my = (e.clientY - r.top) * sc, best = -1, bestD = 36;
    for (var i = 0; i < px.length; i++) {
      var dx = px[i][0] - mx, dy = px[i][1] - my, d = dx * dx + dy * dy;
      if (d < bestD) { best = i; bestD = d; }
    }
    return best;
  }
  canvas.onmousemove = function(e) {
    var i = nearest(e);
    if (i < 0) { tip.style.display = 'none'; return; }
    tip.textContent = P[i][0] + '  APS ' + P[i][3].toFixed(2);
    tip.style.left = e.clientX + 12 + 'px'; tip.style.top = e.clientY + 2 + 'px'; tip.style.display = '';
  };
  canvas.onmouseleave = function() { tip.style.display = 'none'; };
  canvas.onclick = function(e) {
    var i = nearest(e);
    if (i >= 0) location.href = 'BotDetails?game=' + encodeURIComponent(game) + '&name=' + encodeURIComponent(P[i][0]) + extra;
  };
})();
</script>""")
    out.append("</body></html>")
    return "".join(out)


if __name__ == "__main__":
    # three rock-paper-scissors styles on top of a strength ladder: style s beats
    # style s+1 by 10; the clusters must recover the styles, not the strength
    rng = numpy.random.RandomState(1)
    n = 60
    style = numpy.arange(n) % 3
    strength = numpy.linspace(-20, 20, n)
    rps = numpy.where((style[None, :] - style[:, None]) % 3 == 1, 10, 0) - numpy.where((style[:, None] - style[None, :]) % 3 == 1, 10, 0)
    M = 50 + strength[:, None] - strength[None, :] + rps + rng.randn(n, n)  # M[i, j] = i vs j
    A = (M.T + 100 - M) / 2  # A[j, i] = i vs j, APS-symmetric
    numpy.fill_diagonal(A, numpy.nan)
    xy = numpy.array(spectral_embed(A, A + rng.randn(n, n), rng.rand(n, n)))  # style-free survival and variance
    d = ((xy[:, None] - xy[None]) ** 2).sum(2)
    numpy.fill_diagonal(d, numpy.inf)
    pure = (style[d.argmin(1)] == style).mean()
    assert pure > 0.9, pure  # chance is ~0.33
    print("ok", pure)
