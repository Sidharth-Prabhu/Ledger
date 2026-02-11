document.addEventListener('DOMContentLoaded', () => {
  const form       = document.getElementById('uploadForm');
  const dropArea   = document.getElementById('dropArea');
  const fileInput  = document.getElementById('fileInput');
  const fileListEl = document.getElementById('fileList');
  const status     = document.getElementById('status');

  let selectedFiles = [];

  dropArea.addEventListener('click', () => fileInput.click());

  ['dragover', 'dragenter'].forEach(ev => dropArea.addEventListener(ev, e => {
    e.preventDefault();
    dropArea.classList.add('highlight');
  }));

  ['dragleave', 'drop'].forEach(ev => dropArea.addEventListener(ev, e => {
    e.preventDefault();
    dropArea.classList.remove('highlight');
  }));

  dropArea.addEventListener('drop', e => handleFiles(e.dataTransfer.files));

  fileInput.addEventListener('change', () => {
    handleFiles(fileInput.files);
    fileInput.value = '';
  });

  function handleFiles(fileList) {
    Array.from(fileList).forEach(file => {
      if (selectedFiles.some(f => f.name === file.name && f.size === file.size)) return;
      selectedFiles.push(file);

      const li = document.createElement('li');
      li.innerHTML = `
        ${file.name} <small>(${(file.size/1024/1024).toFixed(2)} MB)</small>
        <button type="button" class="remove" title="Remove">×</button>
      `;
      li.querySelector('.remove').onclick = () => {
        selectedFiles = selectedFiles.filter(f => f !== file);
        li.remove();
      };
      fileListEl.appendChild(li);
    });
  }

  form.addEventListener('submit', async e => {
    e.preventDefault();
    status.textContent = '';
    status.className = '';

    if (!selectedFiles.length) {
      status.textContent = 'Select at least one file';
      status.className = 'error';
      return;
    }

    const formData = new FormData(form);
    selectedFiles.forEach(file => formData.append('files', file));

    try {
      status.textContent = `Uploading ${selectedFiles.length} file(s)...`;
      status.className = 'loading';

      const res = await fetch('/upload', { method: 'POST', body: formData });

      console.log('Status:', res.status, 'Content-Type:', res.headers.get('content-type'));

      const text = await res.text();
      console.log('Raw response:', text);

      if (!res.ok) {
        throw new Error(`HTTP ${res.status}: ${text}`);
      }

      const data = JSON.parse(text);

      if (data.status === 'success') {
        status.textContent = 'Upload successful!';
        status.className = 'success';
        selectedFiles = [];
        fileListEl.innerHTML = '';
        form.reset();
      } else {
        status.textContent = data.message || 'Upload failed';
        status.className = 'error';
      }
    } catch (err) {
      console.error(err);
      status.textContent = err.message.includes('JSON') ? 'Server response invalid' : err.message;
      status.className = 'error';
    }
  });
});