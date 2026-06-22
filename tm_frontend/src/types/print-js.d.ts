declare module 'print-js' {
  interface PrintJSOptions {
    printable: string
    type?: 'html' | 'pdf' | 'image' | 'json' | 'raw-html'
    targetStyles?: string[]
    style?: string
    scanStyles?: boolean
    documentTitle?: string
  }

  export default function printJS(options: PrintJSOptions | string): void
}
