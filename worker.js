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

        // כאן משנים ל-grader.py
        const graderText = await (await fetch('grader.py')).text();
        pyodide.FS.writeFile("grader.py", graderText);

        // 3. הרצת פייתון ישירות (וכאן קוראים ל-grader במקום grader3)
        await pyodide.runPythonAsync(`
            import grader
            try:
                config_db = grader.load_config()
                grader.process_submission("${exerciseId}", "submission.zip", config_db)
            except Exception as e:
                print(f"[!] System Error: {e}")
        `);
        
        self.postMessage({ type: 'done' });
    } catch (err) {
        self.postMessage({ type: 'stdout', text: "\n[!] 💥 קריסה פנימית של הבודק:\n" + String(err) });
        self.postMessage({ type: 'done' });
    }
};