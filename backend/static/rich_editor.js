(function () {
    function uploadFile(file) {
        var form = new FormData();
        form.append('file', file);
        return fetch('/upload_attachment', { method: 'POST', body: form })
            .then(function (response) { return response.json(); })
            .then(function (data) {
                if (data.status !== 'ok') {
                    throw new Error(data.message || '上传失败');
                }
                return data;
            });
    }

    function insertNodeAtSelection(node) {
        var selection = window.getSelection();
        if (!selection || !selection.rangeCount) {
            return false;
        }
        var range = selection.getRangeAt(0);
        range.deleteContents();
        range.insertNode(node);
        range.setStartAfter(node);
        range.collapse(true);
        selection.removeAllRanges();
        selection.addRange(range);
        return true;
    }

    function restoreSelection(savedRange) {
        if (!savedRange) {
            return false;
        }
        var selection = window.getSelection();
        if (!selection) {
            return false;
        }
        selection.removeAllRanges();
        selection.addRange(savedRange);
        return true;
    }

    function initRichEditor(config) {
        var editor = document.querySelector(config.editorSelector);
        var hiddenHtml = document.querySelector(config.hiddenHtmlSelector);
        var hiddenText = document.querySelector(config.hiddenTextSelector);
        var insertImageBtn = document.querySelector(config.insertImageBtnSelector);
        var insertFileBtn = document.querySelector(config.insertFileBtnSelector);
        var imageInput = document.querySelector(config.imageInputSelector);
        var fileInput = document.querySelector(config.fileInputSelector);
        var savedRange = null;

        function ensurePlaceholder() {
            if (!editor.innerHTML.trim()) {
                editor.innerHTML = '<p class="placeholder">' + (config.placeholderText || '在这里输入答案，或用工具栏插入图片/文件') + '</p>';
                editor.dataset.hasPlaceholder = '1';
            }
        }

        function clearPlaceholderIfNeeded() {
            if (editor.dataset.hasPlaceholder === '1' || editor.querySelector('.placeholder')) {
                editor.innerHTML = '';
                delete editor.dataset.hasPlaceholder;
            }
        }

        function updateHiddenFields() {
            if (editor.dataset.hasPlaceholder === '1' || editor.querySelector('.placeholder')) {
                hiddenHtml.value = '';
                hiddenText.value = '';
                return;
            }
            var html = editor.innerHTML;
            var text = editor.innerText || editor.textContent || '';
            hiddenHtml.value = html;
            hiddenText.value = text.trim();
        }

        function rememberSelection() {
            var selection = window.getSelection();
            if (selection && selection.rangeCount) {
                savedRange = selection.getRangeAt(0).cloneRange();
            }
        }

        function insertImage(url, filename) {
            clearPlaceholderIfNeeded();
            restoreSelection(savedRange);
            var img = document.createElement('img');
            img.src = url;
            img.alt = filename || '图片';
            img.loading = 'lazy';
            if (!insertNodeAtSelection(img)) {
                editor.appendChild(img);
            }
            editor.appendChild(document.createElement('br'));
            updateHiddenFields();
            editor.focus();
            rememberSelection();
        }

        function insertFile(url, filename) {
            clearPlaceholderIfNeeded();
            restoreSelection(savedRange);
            var chip = document.createElement('span');
            chip.className = 'file-chip';
            var link = document.createElement('a');
            link.href = url;
            link.target = '_blank';
            link.rel = 'noopener noreferrer';
            link.textContent = filename || '下载文件';
            chip.appendChild(link);
            if (!insertNodeAtSelection(chip)) {
                editor.appendChild(chip);
            }
            editor.appendChild(document.createElement('br'));
            updateHiddenFields();
            editor.focus();
            rememberSelection();
        }

        function handleFiles(files, mode) {
            var pending = Array.prototype.slice.call(files || []).map(function (file) {
                return uploadFile(file).catch(function () { return null; });
            });
            return Promise.all(pending).then(function (results) {
                results.forEach(function (data) {
                    if (!data) {
                        return;
                    }
                    if ((mode === 'image') || data.kind === 'image') {
                        insertImage(data.path, data.filename);
                    } else {
                        insertFile(data.path, data.filename);
                    }
                });
            });
        }

        if (editor) {
            ensurePlaceholder();
            editor.addEventListener('focus', clearPlaceholderIfNeeded);
            editor.addEventListener('input', updateHiddenFields);
            editor.addEventListener('keyup', rememberSelection);
            editor.addEventListener('mouseup', rememberSelection);
            editor.addEventListener('blur', updateHiddenFields);
            editor.addEventListener('paste', function () {
                window.setTimeout(updateHiddenFields, 0);
            });
        }

        if (insertImageBtn && imageInput) {
            insertImageBtn.addEventListener('click', function () {
                imageInput.value = '';
                imageInput.click();
            });
            imageInput.addEventListener('change', function () {
                handleFiles(imageInput.files, 'image');
                imageInput.value = '';
            });
        }

        if (insertFileBtn && fileInput) {
            insertFileBtn.addEventListener('click', function () {
                fileInput.value = '';
                fileInput.click();
            });
            fileInput.addEventListener('change', function () {
                handleFiles(fileInput.files, 'file');
                fileInput.value = '';
            });
        }

        updateHiddenFields();
        rememberSelection();
        return {
            editor: editor,
            update: updateHiddenFields,
            setHtml: function (html) {
                editor.innerHTML = html || '';
                updateHiddenFields();
                ensurePlaceholder();
            }
        };
    }

    window.initRichEditor = initRichEditor;
})();
