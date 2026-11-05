// Reduz a imagem no navegador antes do envio (app/images.py só confere tipo e tamanho).
// <input type="file" data-resize="square:256"> recorta o centro em quadrado (foto do perfil);
// data-resize="max:600" mantém a proporção com o lado maior em até 600 px (imagem do torneio).
// data-preview="<id>" mostra o resultado num <img> (e data-hide="<id>" esconde o que estava no
// lugar, como a inicial do nome). Se algo falhar, vai o arquivo original.
(function () {
  document.querySelectorAll('input[type="file"][data-resize]').forEach(function (input) {
    input.addEventListener('change', function () {
      var file = input.files && input.files[0];
      if (!file || !/^image\/(png|jpeg|webp)$/.test(file.type) || !window.DataTransfer) return;
      var parts = input.dataset.resize.split(':'), size = Number(parts[1]) || 600;
      var source = URL.createObjectURL(file);
      var img = new Image();
      img.onload = function () {
        URL.revokeObjectURL(source);
        var sx = 0, sy = 0, sw = img.naturalWidth, sh = img.naturalHeight, w, h;
        if (parts[0] === 'square') {
          var side = Math.min(sw, sh);
          sx = (sw - side) / 2; sy = (sh - side) / 2; sw = sh = side;
          w = h = Math.min(size, side);
        } else {
          var scale = Math.min(1, size / Math.max(sw, sh));
          w = Math.round(sw * scale); h = Math.round(sh * scale);
        }
        var canvas = document.createElement('canvas');
        canvas.width = w; canvas.height = h;
        canvas.getContext('2d').drawImage(img, sx, sy, sw, sh, 0, 0, w, h);
        // WebP mantém a transparência dos escudos; sem suporte, o navegador devolve PNG.
        canvas.toBlob(function (blob) {
          if (!blob) return;
          var transfer = new DataTransfer();
          transfer.items.add(new File([blob], blob.type === 'image/webp' ? 'imagem.webp' : 'imagem.png', { type: blob.type }));
          input.files = transfer.files;
          var preview = input.dataset.preview && document.getElementById(input.dataset.preview);
          if (preview) { preview.src = URL.createObjectURL(blob); preview.hidden = false; }
          var hide = input.dataset.hide && document.getElementById(input.dataset.hide);
          if (hide) hide.hidden = true;
        }, 'image/webp', 0.9);
      };
      img.onerror = function () { URL.revokeObjectURL(source); };
      img.src = source;
    });
  });
})();
