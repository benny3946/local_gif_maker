# Local GIF Maker

A small native Windows desktop app for previewing a local video, choosing a time range, and exporting that section as an animated GIF. Video processing runs locally with FFmpeg.

## Run

From this folder, install the dependencies and start the app:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

The app uses the `ffmpeg` executable on your Windows `PATH`; it does not bundle FFmpeg. If you have just added FFmpeg to `PATH`, close and reopen the app so it sees the updated environment. Python's Tk support must be available in the Python installation.

Pillow provides the `PIL` imports. The `tkinterdnd2` dependency adds drag-and-drop support to the native window. In VS Code, select `.venv\Scripts\python.exe` with **Python: Select Interpreter** if another interpreter is already selected; the workspace settings default to this environment for new selections.

## Use

1. Click **Open video…** and choose a video, or drag one video file anywhere into the app window.
2. The source video scales to fill its preview panel without changing its aspect ratio; playback uses the same aspect ratio and does not add black bars. Preview decoding adapts to the panel size, and playback keeps only the newest frame if rendering falls behind. Scroll over the video to zoom around the pointer; middle-click or use **Reset zoom** to return to fit. Click it or press **Space** to play/pause, or click the ruler or drag the slim blue play pin to seek. The ruler uses the primary video stream's duration, shows time marks from start to finish, and adjusts to the window width. The blue play pin sits in a separate row above the green trim pins so their rectangles do not collide. The blue and green stems extend through the ruler ticks to the selected-range bar; the stem positions mark the exact times. Both pins are drawn on the same ruler, without a separate seek bar. A yellow bar shows the selected range. Drag the solid-green **|◀** start and **▶|** end markers on the ruler to change the trim range; the start and end stems mark their selected times, and the markers cannot cross. Their positions stay synchronized with the time and frame fields. Seeking to the end previews the nearest available frame. The Preview time controls show the current frame and total frame count. Press **Left** or **Right** to seek one frame; holding either key for 0.5 seconds continues stepping through preloaded frames. Set **Frames per preload batch** in the Arrow-key seek panel to a value from 1 to 120 (default 15) to compare responsiveness; during held searching, the app preloads the next batch in the background so seeking can continue across batch boundaries. Larger batches can improve sustained searching but use more memory and may take longer to decode the first frame in each batch. Press **Enter** in any editable field to apply its value and move focus out of the field. Use **Up** and **Down** to jump by the configured seek-step value (1 second by default).
3. Use **Set start here** and **Set end here** during playback or while paused, or edit the start/end times or frame numbers directly. Frame numbers are 1-based and include the selected end frame; changing either representation updates the other.
4. Choose the output frame rate and width, then click **Create GIF…**. The progress bar beside the status message shows FFmpeg's export progress.
5. The resulting animated GIF appears in the preview panel and is saved at the selected path.

Time fields accept either seconds (for example `12.5`) or `HH:MM:SS.mmm`.
