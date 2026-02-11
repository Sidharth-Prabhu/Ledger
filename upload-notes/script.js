document.addEventListener('DOMContentLoaded', () => {
    const dropArea = document.getElementById('dropArea');
    const fileInput = document.getElementById('fileInput');
    const fileList = document.getElementById('fileList');
    const form = document.getElementById('uploadForm');

    let selectedFiles = [];

    dropArea.addEventListener('click', () => fileInput.click());

    dropArea.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropArea.classList.add('highlight');
    });

    dropArea.addEventListener('dragleave', () => {
        dropArea.classList.remove('highlight');
    });

    dropArea.addEventListener('drop', (e) => {
        e.preventDefault();
        dropArea.classList.remove('highlight');
        handleFiles(e.dataTransfer.files);
    });

    fileInput.addEventListener('change', () => {
        handleFiles(fileInput.files);
        fileInput.value = ''; // Allow re-selecting the same files if needed
    });

    function handleFiles(files) {
        for (let file of files) {
            selectedFiles.push(file);
            const li = document.createElement('li');
            li.textContent = file.name;
            fileList.appendChild(li);
        }
    }

    form.addEventListener('submit', async (e) => {
        e.preventDefault();

        const formData = new FormData();
        formData.append('title', document.getElementById('title').value);
        formData.append('subject', document.getElementById('subject').value);

        for (let file of selectedFiles) {
            formData.append('files', file);
        }

        try {
            const response = await fetch('/notes/upload', {
                method: 'POST',
                body: formData
            });

            const data = await response.json();

            if (data.status === 'success') {
                alert('Study materials uploaded successfully!');
                // Reset form
                form.reset();
                fileList.innerHTML = '';
                selectedFiles = [];
            } else {
                alert(`Error: {data.message || 'Upload failed'}`);
            }
        } catch (err) {
            alert(`Error: {err.message}`);
        }
    });
});