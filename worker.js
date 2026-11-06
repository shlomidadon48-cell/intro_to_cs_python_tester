importScripts("https://cdn.jsdelivr.net/pyodide/v0.24.1/full/pyodide.js");

self.onmessage = async (event) => {
    const { zipBuffer, exerciseId } = event.data;

    try {
        // טעינת מנוע פייתון והפניית ההדפסות למסך הראשי
        let pyodide = await loadPyodide({
            stdout: (text) => self.postMessage({ type: 'stdout', text }),
            stderr: (text) => self.postMessage({ type: 'stdout', text })
        });

        // 1. כתיבת קובץ ה-ZIP של הסטודנט למערכת הקבצים הווירטואלית
        pyodide.FS.writeFile("submission.zip", new Uint8Array(zipBuffer));
        
        // 2. משיכת קבצי הקינפוג והבודק שלך מהשרת/התיקייה
        const configText = await (await fetch('config.json')).text();
        pyodide.FS.writeFile("config.json", configText);

        const graderText = await (await fetch('grader3.py')).text();
        pyodide.FS.writeFile("grader3.py", graderText);

        // 3. הרצת פייתון ישירות (עוקף את ה-input הסטנדרטי ב-main)
        await pyodide.runPythonAsync(`
            import grader3
            try:
                config_db = grader3.load_config()
                grader3.process_submission("${exerciseId}", "submission.zip", config_db)
            except Exception as e:
                print(f"[!] System Error: {e}")
        `);
        
        self.postMessage({ type: 'done' });
    } catch (err) {
        self.postMessage({ type: 'stdout', text: "\n[!] 💥 קריסה פנימית של הבודק:\n" + String(err) });
        self.postMessage({ type: 'done' });
    }
};