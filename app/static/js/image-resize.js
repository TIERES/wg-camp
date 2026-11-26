// Recorte da imagem no navegador antes do envio (app/images.py só confere o tipo e o tamanho do
// arquivo; o servidor não mexe em imagem).
//   <input type="file" data-crop="480x600" data-preview="<id>" data-hide="<id>">
// Ao escolher o arquivo, abre uma janela com um quadro na proporção do tamanho final (480 x 600:
// imagem do campeonato e da ISO, em pé 4:5; 256 x 256: foto do perfil, quadrada, com o círculo
// de como ela aparece). A imagem começa no centro, cobrindo o quadro inteiro (ampliada se for
// menor); a pessoa arrasta para escolher o que fica dentro, aproxima com o zoom (barra, roda do
// mouse, pinça no celular ou + e -) e move com as setas. "Usar imagem" grava o recorte no tamanho
// final, em WebP (que mantém a transparência; sem suporte, PNG), no lugar do arquivo escolhido;
// data-preview mostra o resultado num <img> e data-hide esconde o que estava no lugar (a inicial
// do nome). "Cancelar" (ou fechar a janela) volta ao que estava antes. Se algo falhar, vai o
// arquivo original. data-crop-max: bytes no máximo (padrão 1,9 MB).
(function () {
  var counter = 0;

  function cropper(input) {
    var size = input.dataset.crop.split('x'), W = Number(size[0]) || 480, H = Number(size[1]) || W;
    var maxBytes = Number(input.dataset.cropMax) || 1.9 * 1024 * 1024;
    var id = 'crop-' + (++counter);
    var dialog = document.createElement('dialog');
    dialog.className = 'form-dialog crop-dialog';
    dialog.setAttribute('aria-labelledby', id + '-title');
    dialog.innerHTML =
      '<div class="form-dialog-body">' +
      '<h2 id="' + id + '-title">Ajustar a imagem</h2>' +
      '<p class="muted crop-hint">Arraste a imagem para escolher o que fica dentro do quadro. Use o zoom para aproximar.</p>' +
      '<div class="crop-frame' + (W === H ? ' round' : '') + '" tabindex="0" aria-label="Recorte da imagem: arraste ou use as setas para mover, + e - para o zoom"><img alt=""></div>' +
      '<label class="crop-zoom"><span>Zoom</span><input type="range" min="1" max="4" step="0.01" value="1" aria-label="Zoom"></label>' +
      '<p class="muted crop-size">Tamanho final: ' + W + ' × ' + H + ' px</p>' +
      '<div class="card-actions dialog-actions"><button type="button" class="secondary" data-crop-cancel>Cancelar</button>' +
      '<button type="button" data-crop-apply>Usar imagem</button></div></div>';
    document.body.appendChild(dialog);
    var frame = dialog.querySelector('.crop-frame'), view = frame.querySelector('img');
    var zoom = dialog.querySelector('input[type=range]');
    var img = null, fw = 0, fh = 0, base = 1, scale = 1, ox = 0, oy = 0, applied = false, previous = null;

    function layout() {
      fw = Math.min(360, window.innerWidth * 0.8);
      fh = fw * H / W;
      var limit = window.innerHeight * 0.55;
      if (fh > limit) { fh = limit; fw = fh * W / H; }
      frame.style.width = fw + 'px';
      frame.style.height = fh + 'px';
      base = Math.max(fw / img.naturalWidth, fh / img.naturalHeight);
    }
    function clamp() {
      var dw = img.naturalWidth * scale, dh = img.naturalHeight * scale;
      ox = Math.min(0, Math.max(fw - dw, ox));
      oy = Math.min(0, Math.max(fh - dh, oy));
    }
    function draw() {
      clamp();
      view.style.width = img.naturalWidth * scale + 'px';
      view.style.height = img.naturalHeight * scale + 'px';
      view.style.transform = 'translate(' + ox + 'px,' + oy + 'px)';
    }
    // Zoom mantendo parado o ponto (x, y) do quadro (o centro, se não vier).
    function setZoom(value, x, y) {
      value = Math.max(1, Math.min(4, value));
      x = x === undefined ? fw / 2 : x; y = y === undefined ? fh / 2 : y;
      var px = (x - ox) / scale, py = (y - oy) / scale;
      scale = base * value;
      ox = x - px * scale; oy = y - py * scale;
      zoom.value = value;
      draw();
    }

    function open(file) {
      var source = URL.createObjectURL(file), loaded = new Image();
      loaded.onload = function () {
        img = loaded;
        view.src = source;
        applied = false;
        if (!dialog.open) dialog.showModal();
        layout();
        scale = base;
        zoom.value = 1;
        ox = (fw - img.naturalWidth * scale) / 2;  // começa no centro
        oy = (fh - img.naturalHeight * scale) / 2;
        draw();
        frame.focus();
      };
      loaded.onerror = function () { URL.revokeObjectURL(source); };  // não é imagem: vai como está (o servidor recusa)
      loaded.src = source;
    }

    // Arrastar (mouse e toque) e pinça com dois dedos.
    var pointers = {}, last = null;
    frame.addEventListener('pointerdown', function (e) {
      if (!img) return;
      pointers[e.pointerId] = { x: e.clientX, y: e.clientY };
      try { frame.setPointerCapture(e.pointerId); } catch (err) { /* ponteiro já solto */ }
      last = null;
      e.preventDefault();
    });
    frame.addEventListener('pointermove', function (e) {
      var p = pointers[e.pointerId];
      if (!p) return;
      var ids = Object.keys(pointers);
      if (ids.length === 1) {
        ox += e.clientX - p.x; oy += e.clientY - p.y;
        draw();
      } else if (ids.length === 2) {
        var other = pointers[ids[0] == e.pointerId ? ids[1] : ids[0]];
        var distance = Math.hypot(e.clientX - other.x, e.clientY - other.y);
        if (last) {
          var rect = frame.getBoundingClientRect();
          setZoom(Number(zoom.value) * distance / last, (e.clientX + other.x) / 2 - rect.left, (e.clientY + other.y) / 2 - rect.top);
        }
        last = distance;
      }
      p.x = e.clientX; p.y = e.clientY;
    });
    function release(e) { delete pointers[e.pointerId]; last = null; }
    frame.addEventListener('pointerup', release);
    frame.addEventListener('pointercancel', release);
    frame.addEventListener('wheel', function (e) {
      if (!img) return;
      e.preventDefault();
      var rect = frame.getBoundingClientRect();
      setZoom(Number(zoom.value) * (e.deltaY < 0 ? 1.1 : 1 / 1.1), e.clientX - rect.left, e.clientY - rect.top);
    }, { passive: false });
    frame.addEventListener('keydown', function (e) {
      var step = e.shiftKey ? 40 : 10, moves = { ArrowLeft: [step, 0], ArrowRight: [-step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] };
      if (moves[e.key]) { ox += moves[e.key][0]; oy += moves[e.key][1]; draw(); e.preventDefault(); }
      else if (e.key === '+' || e.key === '=') { setZoom(Number(zoom.value) * 1.1); e.preventDefault(); }
      else if (e.key === '-') { setZoom(Number(zoom.value) / 1.1); e.preventDefault(); }
    });
    zoom.addEventListener('input', function () { setZoom(Number(zoom.value)); });
    window.addEventListener('resize', function () {
      if (!dialog.open || !img) return;
      var value = Number(zoom.value), cx = (fw / 2 - ox) / scale, cy = (fh / 2 - oy) / scale;
      layout();
      scale = base * value;
      ox = fw / 2 - cx * scale; oy = fh / 2 - cy * scale;
      draw();
    });

    // "Usar imagem": o que está no quadro, no tamanho final.
    function apply() {
      var canvas = document.createElement('canvas');
      canvas.width = W; canvas.height = H;
      var ctx = canvas.getContext('2d');
      ctx.imageSmoothingQuality = 'high';
      ctx.drawImage(img, -ox / scale, -oy / scale, fw / scale, fh / scale, 0, 0, W, H);
      var qualities = [0.9, 0.82, 0.72, 0.6];
      (function encode(i) {
        canvas.toBlob(function (blob) {
          if (!blob) { dialog.close(); return; }
          if (blob.size > maxBytes && i + 1 < qualities.length) { encode(i + 1); return; }
          var transfer = new DataTransfer();
          transfer.items.add(new File([blob], blob.type === 'image/webp' ? 'imagem.webp' : 'imagem.png', { type: blob.type }));
          input.files = transfer.files;
          previous = input.files;
          var preview = input.dataset.preview && document.getElementById(input.dataset.preview);
          if (preview) { preview.src = URL.createObjectURL(blob); preview.hidden = false; }
          var hide = input.dataset.hide && document.getElementById(input.dataset.hide);
          if (hide) hide.hidden = true;
          applied = true;
          dialog.close();
          input.dispatchEvent(new CustomEvent('crop:done', { bubbles: true }));
        }, 'image/webp', qualities[i]);
      })(0);
    }
    dialog.querySelector('[data-crop-apply]').addEventListener('click', apply);
    dialog.querySelector('[data-crop-cancel]').addEventListener('click', function () { dialog.close(); });
    // Fechou sem "Usar imagem" (Cancelar, Esc ou clique fora): volta ao arquivo de antes.
    dialog.addEventListener('close', function () {
      if (applied) return;
      if (previous) input.files = previous;
      else input.value = '';
    });

    input.addEventListener('change', function () {
      var file = input.files && input.files[0];
      if (!file || !/^image\/(png|jpeg|webp)$/.test(file.type) || !window.DataTransfer) return;
      open(file);
    });
  }

  document.querySelectorAll('input[type="file"][data-crop]').forEach(cropper);
})();
