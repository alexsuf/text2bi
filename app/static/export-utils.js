// =============================================
// ОБЩИЕ ФУНКЦИИ ЭКСПОРТА ДЛЯ CHAT И TABLE
// =============================================

function formatDateForFilename(date) {
    const y = date.getFullYear();
    const m = String(date.getMonth() + 1).padStart(2, '0');
    const d = String(date.getDate()).padStart(2, '0');
    const h = String(date.getHours()).padStart(2, '0');
    const min = String(date.getMinutes()).padStart(2, '0');
    const s = String(date.getSeconds()).padStart(2, '0');
    return `${y}-${m}-${d} ${h}-${min}-${s}`;
}

function cleanMarkdownContent(content) {
    if (!content) return '';
    
    return content
        .replace(/\*\*/g, '')
        .replace(/\*/g, '')
        .replace(/__/g, '')
        .replace(/_/g, '')
        .replace(/~~/g, '')
        .replace(/`/g, '')
        .replace(/^#+\s*/gm, '')
        .replace(/^>\s*/gm, '')
        .replace(/^[\-*+]\s*/gm, '')
        .replace(/^\d+\.\s*/gm, '')
        .replace(/!\$\$([\s\S]*?)\]\$[^)]*$/g, '$1')
        .replace(/\$\$([\s\S]*?)\]\$[^)]*$/g, '$1')
        .replace(/\n\s*\n\s*\n/g, '\n\n');
}

function downloadAsText(content, filename, sourceType = 'text') {
    if (!content) {
        alert('Нет содержимого для скачивания');
        return;
    }

    let cleanContent = sourceType === 'markdown' ? cleanMarkdownContent(content) : content;
    
    const dateStr = formatDateForFilename(new Date());
    const finalFilename = `${dateStr} - ${filename}`;
    const blob = new Blob([cleanContent], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = finalFilename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

function downloadTableAsText(tableColumns, tableRows, filename = 'table.txt') {
    if (!tableColumns || !tableColumns.length || !tableRows || !tableRows.length) {
        alert('Нет данных таблицы для скачивания');
        return;
    }

    const header = tableColumns.join('\t');
    const rowsText = tableRows
        .map(row => row.map(cell => cell === null || cell === undefined ? '' : String(cell).replace(/\t/g, ' ')).join('\t'))
        .join('\n');
    const content = `${header}\n${rowsText}`;

    downloadAsText(content, filename, 'table');
}

function downloadDocx(payload, filename) {
    fetch('/download_chat/docx', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    })
    .then(res => {
        if (!res.ok) throw new Error('Ошибка сервера');
        return res.blob();
    })
    .then(blob => {
        const dateStr = formatDateForFilename(new Date());
        const finalFilename = `${dateStr} - ${filename}`;
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = finalFilename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    })
    .catch(err => {
        console.error('Ошибка скачивания DOCX:', err);
        alert('Ошибка при формировании DOCX: ' + err.message);
    });
}

function downloadExcel(tableColumns, tableRows, filename = 'table.xlsx') {
    if (!tableColumns || !tableColumns.length || !tableRows || !tableRows.length) {
        alert('Нет данных для экспорта в Excel');
        return;
    }

    if (!window.XLSX) {
        alert('Не удалось загрузить библиотеку для Excel. Попробуйте снова позже.');
        return;
    }

    const worksheetData = [tableColumns].concat(tableRows.map(row => row.map(v => v === null || v === undefined ? '' : v)));
    const ws = XLSX.utils.aoa_to_sheet(worksheetData);
    const wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, ws, 'Sheet1');
    const wbout = XLSX.write(wb, { bookType: 'xlsx', type: 'array' });
    const blob = new Blob([wbout], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' });
    
    const dateStr = formatDateForFilename(new Date());
    const finalFilename = `${dateStr} - ${filename}`;
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = finalFilename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}
