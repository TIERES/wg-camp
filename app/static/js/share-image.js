// Cartão da imagem do campeonato para o link compartilhado (WhatsApp, Telegram, Discord...).
// A imagem é guardada em WebP (image-resize.js), que esses aplicativos nem sempre mostram, e
// em pé; eles querem JPEG ou PNG deitado, 1200 x 630. Como o servidor não mexe em imagem
// (app/images.py), quem monta o cartão é o navegador de quem gerencia o campeonato: a imagem
// inteira no meio, sobre ela mesma ampliada, desfocada e escurecida, e envia em JPEG
// (tournaments.tournament_share_image). Roda só enquanto o campeonato não tem o cartão
// (tournaments/_share_image.html); se algo falhar, tenta de novo na próxima visita.
//   <script src="share-image.js" data-image="/imagens/torneio/x.webp" data-origin="x.webp"
//           data-post="/torneios/copa/imagem-compartilhar" data-csrf="...">
(function () {
  var script = document.currentScript;
  if (!script || !window.fetch || !window.FormData) return;
  var data = script.dataset;
  var W = 1200, H = 630, MARGIN = 30;
  var MAX_BYTES = 280 * 1024;  // o WhatsApp deixa de mostrar imagem grande (~300 KB)
  var QUALITIES = [0.88, 0.8, 0.7, 0.6];

  function drawScaled(ctx, img, scale) {
    var w = img.naturalWidth * scale, h = img.naturalHeight * scale;
    ctx.drawImage(img, (W - w) / 2, (H - h) / 2, w, h);
  }

  function render(img) {
    var canvas = document.createElement('canvas');
    canvas.width = W; canvas.height = H;
    var ctx = canvas.getContext('2d');
    ctx.fillStyle = '#0b1220';  // o fundo da imagem no site (.t-image): vale para a transparente
    ctx.fillRect(0, 0, W, H);
    if ('filter' in ctx) {  // sem filtro no canvas (Safari antigo) fica o fundo liso
      ctx.filter = 'blur(30px) brightness(.5)';
      drawScaled(ctx, img, Math.max(W / img.naturalWidth, H / img.naturalHeight) * 1.15);
      ctx.filter = 'none';
    }
    drawScaled(ctx, img, Math.min((W - 2 * MARGIN) / img.naturalWidth, (H - 2 * MARGIN) / img.naturalHeight));
    return canvas;
  }

  function send(blob) {
    var form = new FormData();
    form.append('csrf_token', data.csrf);
    form.append('origem', data.origin);
    form.append('image', blob, 'compartilhar.jpg');
    fetch(data.post, { method: 'POST', body: form, credentials: 'same-origin' }).catch(function () {});
  }

  // A melhor qualidade que cabe no limite.
  function encode(canvas, index) {
    canvas.toBlob(function (blob) {
      if (!blob || blob.type !== 'image/jpeg') return;
      if (blob.size > MAX_BYTES && index + 1 < QUALITIES.length) encode(canvas, index + 1);
      else send(blob);
    }, 'image/jpeg', QUALITIES[index]);
  }

  var img = new Image();
  img.onload = function () {
    if (!img.naturalWidth || !img.naturalHeight) return;
    try { encode(render(img), 0); } catch (e) { /* tenta de novo na próxima visita */ }
  };
  img.src = data.image;
})();
