"use client";

import { useState } from "react";
import axios from "axios";

export default function Home() {
  console.log("Home component rendered");
  const [file, setFile] = useState(null);

  const uploadVideo = async () => {

    const data = new FormData();

    data.append("file", file);

    const response = await axios.post(
      "http://localhost:8000/upload",
      data
    );

    alert("Upload Success");
    console.log(response.data);
  };

  return (
    <div style={{ padding: 20 }}>
      <h1>AI Video Editor</h1>

      <input
        type="file"
        onChange={(e) =>
          setFile(e.target.files[0])
        }
      />

      <button onClick={uploadVideo}>
        Upload
      </button>
    </div>
  );
}
