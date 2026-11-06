importScripts("https://cdn.jsdelivr.net/pyodide/v0.24.1/full/pyodide.js");

self.onmessage = async (event) => {
    // הוספנו כאן את קבלת fileName
    const { zipBuffer, exerciseId, fileName } = event.data;

    try {
        let pyodide = await loadPyodide({
            stdout: (text) => self.postMessage({ type: 'stdout', text }),
            stderr: (text) => self.postMessage({ type: 'stdout', text })
        });

        // 1. כתיבת קובץ ה-ZIP של הסטודנט תחת השם המקורי שלו
        pyodide.FS.writeFile(fileName, new Uint8Array(zipBuffer));
        
        // 2. משיכת קבצי הקינפוג והבודק
        const configText = await (await fetch('config.json')).text();
        pyodide.FS.writeFile("config.json", configText);

        const graderText = await (await fetch('grader.py')).text();
        pyodide.FS.writeFile("grader.py", graderText);

        // 3. הרצת פייתון עם השם המקורי של הקובץ
        await pyodide.runPythonAsync(`
            import grader
            try:
                config_db = grader.load_config()
                # העברנו את שם הקובץ במקום השם הקשיח
                grader.process_submission("${exerciseId}", "${fileName}", config_db)
            except Exception as e:
                print(f"[!] System Error: {e}")
        `);
        
        self.postMessage({ type: 'done' });
    } catch (err) {
        self.postMessage({ type: 'stdout', text: "\n[!] 💥 קריסה פנימית של הבודק:\n" + String(err) });
        self.postMessage({ type: 'done' });
    }
};